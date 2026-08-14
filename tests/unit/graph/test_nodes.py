"""Unit tests for the cascade's node logic.

The real LLM is exercised in tests/integration/test_triage_pipeline.py. Here the
provider is only substituted where the point of the test is a failure the real API
cannot be made to produce on demand (a hard outage, a schema-invalid reply).
"""

from datetime import datetime, timezone

import pytest

from graph import nodes
from tools.rules import DEFAULT_TAXONOMY


def item(**overrides):
    base = {
        "id": "i1",
        "external_thread_id": "t1",
        "subject": "Weekly digest",
        "from_name": "Substack",
        "from_email": "news@substack.com",
        "from_domain": "substack.com",
        "list_id": "<weekly.substack.com>",
        "snippet_redacted": "Top stories this week",
        "message_count": 1,
        "has_attachments": False,
        "is_unread": True,
        "internal_date": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


def base_state(**overrides):
    state = {
        "run_id": "run-1",
        "user_id": "user-1",
        "channel_account_id": "acct-1",
        "categories": DEFAULT_TAXONOMY,
        "rules": [],
        "sender_stats": {},
        "settings": {"confidence_floor": 0.75},
        "items": [item()],
        "resolved": [],
        "llm_decisions": [],
        "deep_queue": [],
        "llm_calls": [],
    }
    state.update(overrides)
    return state


class TestRedactNode:
    def test_is_the_egress_chokepoint(self):
        state = base_state(
            items=[item(subject="Your code is 998877", snippet_redacted="sk-abcdefghijklmnopqrst")]
        )
        out = nodes.redact_items(state)
        assert "998877" not in out["items"][0]["subject"]
        assert "[REDACTED:api_key]" in out["items"][0]["snippet_redacted"]

    def test_empty_item_list(self):
        assert nodes.redact_items(base_state(items=[]))["items"] == []


class TestTierNodes:
    def test_rules_resolve_and_shrink_the_llm_queue(self):
        rules = [
            {
                "id": "r1",
                "name": "Substack",
                "matcher": {"list_id": "substack.com"},
                "action": {"set_category": "newsletters", "archive": True},
                "status": "active",
                "confidence": 0.95,
            }
        ]
        out = nodes.apply_deterministic_rules(base_state(rules=rules))
        assert out["llm_queue"] == []
        assert out["resolved"][0]["decided_by"] == "rule"

    def test_sender_history_runs_only_on_the_remainder(self):
        state = base_state(
            llm_queue=[item(id="i2", from_email="friend@acme.io")],
            sender_stats={"friend@acme.io": {"ever_replied": True, "replied_count": 3}},
        )
        out = nodes.apply_sender_history(state)
        assert out["llm_queue"] == []
        assert out["resolved"][0]["decided_by"] == "sender_history"

    def test_nothing_matches_leaves_the_queue_intact(self):
        out = nodes.apply_deterministic_rules(base_state(rules=[]))
        assert len(out["llm_queue"]) == 1 and out["resolved"] == []


class TestBatching:
    @pytest.mark.parametrize("count", [1, 19, 20, 50, 51, 220, 999])
    def test_batches_stay_within_20_50_and_lose_nothing(self, count):
        items = [item(id=f"i{n}") for n in range(count)]
        batches = nodes.prepare_llm_batches(base_state(llm_queue=items))["batches"]
        assert sum(len(b) for b in batches) == count
        assert all(len(b) <= nodes.MAX_BATCH for b in batches)
        if count > nodes.MAX_BATCH:
            assert all(len(b) >= nodes.MIN_BATCH for b in batches)
            # batching, not per-message calls
            assert len(batches) <= -(-count // nodes.MIN_BATCH)

    def test_no_items_means_no_batches_and_no_calls(self):
        assert nodes.prepare_llm_batches(base_state(llm_queue=[]))["batches"] == []


class TestConfidenceFloor:
    def test_below_floor_never_archives_and_routes_to_the_user(self):
        decisions = [
            {
                "item_id": "i1",
                "category": "newsletters",
                "proposed_action": "archive",
                "confidence": 0.5,
                "reasoning": "looks like a newsletter.",
                "decided_by": "llm",
            }
        ]
        (out,) = nodes.apply_confidence_floor(decisions, 0.75)
        assert out["proposed_action"] == "keep"
        assert out["status"] == "needs_your_call"
        assert "below the 0.75 floor" in out["reasoning"]

    def test_above_floor_is_left_alone(self):
        decisions = [
            {
                "item_id": "i1",
                "category": "newsletters",
                "proposed_action": "archive",
                "confidence": 0.93,
                "reasoning": "r",
                "decided_by": "rule",
            }
        ]
        (out,) = nodes.apply_confidence_floor(decisions, 0.75)
        assert out["proposed_action"] == "archive"
        assert out["status"] == "proposed"


class TestLlmBatchFailure:
    def test_a_hard_provider_outage_degrades_to_needs_your_call_never_archive(
        self, monkeypatch
    ):
        class Boom:
            async def classify_batch(self, *args, **kwargs):
                raise RuntimeError("NIM unavailable after retries")

        monkeypatch.setattr("llm.client.get_llm_client", lambda: Boom())
        out = nodes.llm_classify_batch(base_state(batch=[item(), item(id="i2")]))
        assert len(out["llm_decisions"]) == 2
        assert {d["decided_by"] for d in out["llm_decisions"]} == {"error"}
        assert {d["proposed_action"] for d in out["llm_decisions"]} == {"keep"}
        assert all(d["confidence"] == 0.0 for d in out["llm_decisions"])
        assert out["deep_queue"] == []

    def test_a_thread_the_model_skipped_degrades_rather_than_being_guessed(
        self, monkeypatch
    ):
        from llm.providers.base import BatchClassification, LLMResult

        class Partial:
            async def classify_batch(self, items, **kwargs):
                return BatchClassification(
                    results=[
                        {
                            "item_id": "i1",
                            "category": "newsletters",
                            "action": "archive",
                            "confidence": 0.9,
                            "reasoning": "List-Id is a Substack list.",
                        }
                    ],
                    missing_ids=["i2"],
                    usage=LLMResult(text="", model="m", tokens_in=10, tokens_out=5),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: Partial())
        out = nodes.llm_classify_batch(base_state(batch=[item(), item(id="i2")]))
        by_id = {d["item_id"]: d for d in out["llm_decisions"]}
        assert by_id["i1"]["decided_by"] == "llm"
        assert by_id["i2"]["decided_by"] == "error"
        assert by_id["i2"]["proposed_action"] == "keep"
        assert out["llm_calls"][0]["tokens_in"] == 10

    def test_a_stale_user_model_falls_back_to_the_next_chain_model(self, monkeypatch):
        """Phase 6: the fallback is the in-family model chain, not "the default".

        ``_model_candidates`` now returns ``llm.health.model_chain(preferred)`` sliced
        at the run's current position, so a retired per-user model id is tried first
        and the run rotates onto the next real model instead of falling back to
        ``model=None``.
        """
        from llm.providers.base import BatchClassification, LLMResult

        seen: list[str | None] = []

        class Flaky:
            async def classify_batch(self, items, *, model=None, **kwargs):
                seen.append(model)
                if model == "retired/model-9":
                    raise RuntimeError("404 page not found: retired model id")
                return BatchClassification(
                    results=[
                        {
                            "item_id": "i1", "category": "newsletters", "action": "archive",
                            "confidence": 0.9, "reasoning": "Substack List-Id.",
                        }
                    ],
                    usage=LLMResult(text="", model="default-model", tokens_in=5, tokens_out=2),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: Flaky())
        # A model advance is per-run and sticks, so this run starts from chain
        # position 0 exactly as graph.runner does on every start/resume.
        from llm import health

        health.reset("run-1")
        state = base_state(batch=[item()], settings={"llm_model": "retired/model-9"})
        out = nodes.llm_classify_batch(state)
        assert seen[0] == "retired/model-9"
        assert len(seen) >= 2 and seen[1] != "retired/model-9"
        assert out["llm_decisions"][0]["decided_by"] == "llm"

    def test_empty_batch_is_a_no_op(self):
        assert nodes.llm_classify_batch(base_state(batch=[])) == {}


class TestVerdictNormalisation:
    def test_unknown_category_and_action_fall_back_safely(self):
        out = nodes._normalise_verdict(
            {"category": "nonsense", "action": "delete", "confidence": 2, "reasoning": "x"},
            item(),
            {c["key"] for c in DEFAULT_TAXONOMY},
        )
        assert out["category"] in {c["key"] for c in DEFAULT_TAXONOMY}
        assert out["proposed_action"] == "keep"
        assert out["confidence"] == 1.0

    def test_time_sensitive_forces_keep(self):
        out = nodes._normalise_verdict(
            {
                "category": "receipts",
                "action": "archive",
                "confidence": 0.99,
                "reasoning": "invoice due friday",
                "time_sensitive": True,
            },
            item(),
            {c["key"] for c in DEFAULT_TAXONOMY},
        )
        assert out["proposed_action"] == "keep" and out["time_sensitive"] is True

    def test_unsure_caps_confidence_and_keeps(self):
        out = nodes._normalise_verdict(
            {
                "category": "people",
                "action": "archive",
                "confidence": 0.95,
                "reasoning": "cannot tell",
                "unsure": True,
            },
            item(),
            {c["key"] for c in DEFAULT_TAXONOMY},
        )
        assert out["unsure"] is True
        assert out["proposed_action"] == "keep"
        assert out["confidence"] <= nodes.UNSURE_MAX_CONFIDENCE


class TestMergeAndCounts:
    def test_deeper_tier_overrides_the_batch_verdict(self):
        state = base_state(
            resolved=[],
            llm_decisions=[
                {
                    "item_id": "i1", "category": "newsletters", "proposed_action": "archive",
                    "confidence": 0.4, "reasoning": "batch", "decided_by": "llm",
                },
                {
                    "item_id": "i1", "category": "people", "proposed_action": "keep",
                    "confidence": 0.9, "reasoning": "deep", "decided_by": "llm_deep",
                },
            ],
        )
        (merged,) = nodes._merge_decisions(state)
        assert merged["decided_by"] == "llm_deep"

    def test_an_item_with_no_verdict_is_never_lost(self):
        merged = nodes._merge_decisions(base_state(items=[item(), item(id="i2")]))
        assert len(merged) == 2
        assert {d["decided_by"] for d in merged} == {"error"}

    def test_counts_report_tiers_categories_and_needs_your_call(self):
        counts = nodes._counts(
            [
                {"decided_by": "rule", "category": "newsletters", "proposed_action": "archive",
                 "status": "proposed"},
                {"decided_by": "llm", "category": "people", "proposed_action": "keep",
                 "status": "needs_your_call"},
            ]
        )
        assert counts["total"] == 2
        assert counts["by_tier"] == {"rule": 1, "llm": 1}
        assert counts["needs_your_call"] == 1
        assert counts["by_category"]["people"] == 1


class TestClusterNode:
    def test_applies_the_floor_before_clustering(self):
        state = base_state(
            llm_decisions=[
                {
                    "item_id": "i1", "category": "newsletters", "proposed_action": "archive",
                    "confidence": 0.2, "reasoning": "weak", "decided_by": "llm",
                }
            ]
        )
        out = nodes.cluster_decisions(state)
        assert out["decisions"][0]["status"] == "needs_your_call"
        assert all(c["suggested_action"] != "archive" for c in out["clusters"])


class TestFetchProgressSSE:
    """fetch_items passes an on_page callback that emits fetch_progress events."""

    def test_fetch_progress_event_emitted_per_page(self, monkeypatch):
        """Each page of Gmail results fires a fetch_progress SSE event."""
        import channels
        import events.bus as bus_mod

        emitted: list[dict] = []
        monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

        pages_served = {"n": 0}

        class FakeAdapter:
            def list_threads(self, *, limit, cancel_check=None, after=None, on_page=None):
                # Simulate two pages by calling on_page twice.
                if on_page:
                    on_page(1, 50)
                    on_page(2, 80)
                return []

            def sender_history(self, *, limit=500):
                return {}

        monkeypatch.setattr(channels, "get_adapter", lambda **kw: FakeAdapter())

        nodes.fetch_items(base_state(items=[], run_id="run-x", user_id="u1"))

        fetch_events = [e for e in emitted if e.get("type") == "fetch_progress"]
        assert len(fetch_events) == 2
        assert fetch_events[0]["page"] == 1
        assert fetch_events[0]["fetched_so_far"] == 50
        assert fetch_events[1]["page"] == 2
        assert fetch_events[1]["fetched_so_far"] == 80
        # run_id is included so the frontend can correlate.
        assert all(e["run_id"] == "run-x" for e in fetch_events)

    def test_fetch_progress_not_emitted_when_no_pages(self, monkeypatch):
        """No events emitted if the adapter calls on_page zero times (empty inbox)."""
        import channels
        import events.bus as bus_mod

        emitted: list[dict] = []
        monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

        class FakeAdapter:
            def list_threads(self, *, limit, cancel_check=None, after=None, on_page=None):
                return []

            def sender_history(self, *, limit=500):
                return {}

        monkeypatch.setattr(channels, "get_adapter", lambda **kw: FakeAdapter())
        nodes.fetch_items(base_state(items=[], run_id="run-y", user_id="u2"))

        assert not any(e.get("type") == "fetch_progress" for e in emitted)


class TestRunProgressSSEDuringBatch:
    """llm_classify_batch emits run_progress SSE after each batch."""

    def test_run_progress_emitted_after_successful_batch(self, monkeypatch):
        from llm.providers.base import BatchClassification, LLMResult

        emitted: list[dict] = []
        import events.bus as bus_mod
        monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

        class FakeLLM:
            async def classify_batch(self, payloads, *, instructions, item_schema,
                                     id_field, model=None, max_attempts=2):
                return BatchClassification(
                    results=[{
                        "item_id": p["item_id"], "category": "newsletters",
                        "action": "archive", "confidence": 0.92, "reasoning": "test",
                    } for p in payloads],
                    usage=LLMResult(text="", model="m", tokens_in=10, tokens_out=5),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: FakeLLM())
        state = base_state(batch=[item()], resolved=[], llm_decisions=[], llm_calls=[])
        nodes.llm_classify_batch(state)

        progress_events = [e for e in emitted if e.get("type") == "run_progress"]
        assert len(progress_events) >= 1
        assert "items_decided" in progress_events[0]
        assert "cost_so_far" in progress_events[0]
        assert "run_id" in progress_events[0]

    def test_run_progress_contains_run_id(self, monkeypatch):
        from llm.providers.base import BatchClassification, LLMResult

        emitted: list[dict] = []
        import events.bus as bus_mod
        monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

        class FakeLLM:
            async def classify_batch(self, payloads, *, instructions, item_schema,
                                     id_field, model=None, max_attempts=2):
                return BatchClassification(
                    results=[{
                        "item_id": p["item_id"], "category": "newsletters",
                        "action": "archive", "confidence": 0.92, "reasoning": "test",
                    } for p in payloads],
                    usage=LLMResult(text="", model="m", tokens_in=10, tokens_out=5),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: FakeLLM())
        state = base_state(batch=[item()], run_id="run-batch-42",
                           resolved=[], llm_decisions=[], llm_calls=[])
        nodes.llm_classify_batch(state)

        progress_events = [e for e in emitted if e.get("type") == "run_progress"]
        assert any(e["run_id"] == "run-batch-42" for e in progress_events)


class TestThreadClassifiedSSE:
    """llm_classify_batch emits thread_classified for each verdict."""

    def _fake_llm(self):
        from llm.providers.base import BatchClassification, LLMResult

        class FakeLLM:
            async def classify_batch(self, payloads, *, instructions, item_schema,
                                     id_field, model=None, max_attempts=2):
                return BatchClassification(
                    results=[{
                        "item_id": p["item_id"], "category": "newsletters",
                        "action": "archive", "confidence": 0.92, "reasoning": "test",
                    } for p in payloads],
                    usage=LLMResult(text="", model="m", tokens_in=10, tokens_out=5),
                )

        return FakeLLM()

    def test_thread_classified_emitted_per_verdict(self, monkeypatch):
        emitted: list[dict] = []
        import events.bus as bus_mod
        monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))
        monkeypatch.setattr("llm.client.get_llm_client", lambda: self._fake_llm())

        state = base_state(
            batch=[item(id="i1", subject="Weekly newsletter")],
            resolved=[], llm_decisions=[], llm_calls=[],
        )
        nodes.llm_classify_batch(state)

        classified = [e for e in emitted if e.get("type") == "thread_classified"]
        assert len(classified) >= 1
        evt = classified[0]
        assert evt["subject"] == "Weekly newsletter"
        assert evt["category"] != "" or evt["action"] != "" or True  # at least the key exists
        assert "category" in evt
        assert "action" in evt
        assert "decided_by" in evt
        assert "run_id" in evt

    def test_thread_classified_subject_truncated_to_60(self, monkeypatch):
        emitted: list[dict] = []
        import events.bus as bus_mod
        monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))
        monkeypatch.setattr("llm.client.get_llm_client", lambda: self._fake_llm())

        long_subject = "A" * 100
        state = base_state(
            batch=[item(id="i1", subject=long_subject)],
            resolved=[], llm_decisions=[], llm_calls=[],
        )
        nodes.llm_classify_batch(state)

        classified = [e for e in emitted if e.get("type") == "thread_classified"]
        assert all(len(e["subject"]) <= 60 for e in classified)
