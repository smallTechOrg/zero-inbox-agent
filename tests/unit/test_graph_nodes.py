"""triage-graph slice — node behaviour through the compiled graph (fakes only).

`make_fake_deps` is shared by the other test_graph_* files. It records a single
chronological `timeline` so ordering invariants (decision persisted before its
mutation; audit-choke-point call per mutation) are assertable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from graph.nodes import TriageDeps
from graph.runner import run_triage
from graph.state import ClassifierView, ClassifyOutcome, Decision, TaxonomyEntry

TAXONOMY = [
    TaxonomyEntry(id="c-fin", name="Finance", description="invoices, receipts", rule="label_only"),
    TaxonomyEntry(id="c-news", name="Newsletters", description="bulk newsletters", rule="label_and_archive"),
    TaxonomyEntry(id="c-notif", name="Notifications", description="automated alerts", rule="label_and_archive"),
    TaxonomyEntry(id="c-pers", name="Personal", description="real people", rule="label_only"),
    TaxonomyEntry(id="c-rev", name="Needs review", description="uncertain", rule="label_only", is_needs_review=True),
]

BY_NAME = {t.name: t for t in TAXONOMY}


def view(i: int, subject: str = "", sender: str = "") -> ClassifierView:
    return ClassifierView(
        thread_id=f"t{i}",
        subject=subject or f"Subject {i}",
        sender_address=sender or f"sender{i}@example.com",
    )


def outcome_for(views, mapping: dict[str, tuple[str, float, str]], **kw) -> ClassifyOutcome:
    results = [
        {"thread_id": v.thread_id, "category": mapping[v.thread_id][0],
         "confidence": mapping[v.thread_id][1], "reason": mapping[v.thread_id][2]}
        for v in views
        if v.thread_id in mapping
    ]
    kw.setdefault("tokens_in", 100)
    kw.setdefault("tokens_out", 50)
    kw.setdefault("est_cost_usd", 0.001)
    return ClassifyOutcome(results=results, **kw)


@dataclass
class FakeWorld:
    deps: TriageDeps = None  # type: ignore[assignment]
    timeline: list[tuple[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    saved: list[Decision] = field(default_factory=list)
    mutations: list[dict[str, Any]] = field(default_factory=list)
    run_updates: list[dict[str, Any]] = field(default_factory=list)
    classify_batches: list[list[ClassifierView]] = field(default_factory=list)

    def events_of(self, type_: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == type_]


def make_fake_deps(
    *,
    threads: list[ClassifierView],
    classify_fn=None,
    decided: set[str] | None = None,
    pending: list[Decision] | None = None,
    taxonomy: list[TaxonomyEntry] | None = None,
    existing_mutations: set[tuple[str, str, str | None]] | None = None,
    batch_size: int = 25,
    fetch_fn=None,
    apply_fn=None,
    update_run_fn=None,
) -> FakeWorld:
    world = FakeWorld()
    tax = taxonomy if taxonomy is not None else TAXONOMY
    existing = existing_mutations or set()

    def fetch(user_id, limit):
        return threads[:limit]

    def classify(views, taxonomy_arg):
        world.classify_batches.append(list(views))
        if classify_fn is None:
            return outcome_for(views, {v.thread_id: ("Finance", 0.9, "obvious invoice") for v in views})
        return classify_fn(views, taxonomy_arg)

    def save_decision(user_id, run_id, decision):
        world.saved.append(decision)
        world.timeline.append(("decision", decision.thread_id))

    def apply_mutation(user_id, run_id, thread_id, action, label_name, reason):
        if (thread_id, action, label_name) in existing:
            return False
        if apply_fn is not None:
            apply_fn(thread_id, action, label_name)
        m = {"thread_id": thread_id, "action": action, "label_name": label_name,
             "reason": reason, "run_id": run_id}
        world.mutations.append(m)
        world.timeline.append(("mutation", (thread_id, action, label_name)))
        return True

    def publish_event(user_id, run_id, type_, sentence, detail):
        world.events.append({"type": type_, "sentence": sentence, "detail": detail})
        world.timeline.append(("event", type_))

    def update_run(run_id, **fields):
        if update_run_fn is not None:
            update_run_fn(run_id, **fields)
        world.run_updates.append({"run_id": run_id, **fields})

    world.deps = TriageDeps(
        fetch_inbox_threads=fetch_fn or fetch,
        decided_thread_ids=lambda user_id: set(decided or set()),
        pending_decisions=lambda user_id: list(pending or []),
        load_taxonomy=lambda user_id: list(tax),
        save_decision=save_decision,
        classify=classify,
        apply_mutation=apply_mutation,
        publish_event=publish_event,
        update_run=update_run,
        batch_size=batch_size,
    )
    return world


def run(world: FakeWorld, chunk_limit: int = 50):
    return run_triage("u1", "r1", chunk_limit=chunk_limit, deps=world.deps)


# ------------------------------------------------------------------ happy path


class TestHappyPath:
    def test_full_chunk_labels_archives_counts_and_events(self):
        threads = [view(1, "ACME invoice #42", "billing@acme.com"),
                   view(2, "Weekly digest", "news@letters.com"),
                   view(3, "Hey, dinner Friday?", "friend@gmail.com")]
        mapping = {"t1": ("Finance", 0.95, "invoice from a vendor"),
                   "t2": ("Newsletters", 0.9, "bulk newsletter"),
                   "t3": ("Personal", 0.85, "personal conversation")}
        world = make_fake_deps(threads=threads,
                               classify_fn=lambda v, t: outcome_for(v, mapping))
        summary = run(world)

        assert summary["status"] == "completed"
        assert summary["threads_decided"] == 3
        assert summary["counts"] == {"Finance": 1, "Newsletters": 1, "Personal": 1}
        assert summary["error"] is None

        # every thread got its category label; the archive rule removed INBOX
        muts = {(m["thread_id"], m["action"], m["label_name"]) for m in world.mutations}
        assert ("t1", "add_label", "ZI/Finance") in muts
        assert ("t2", "add_label", "ZI/Newsletters") in muts
        assert ("t2", "remove_inbox", None) in muts
        assert ("t3", "add_label", "ZI/Personal") in muts
        assert ("t1", "remove_inbox", None) not in muts  # label_only never archives
        assert ("t3", "remove_inbox", None) not in muts

        # one decision event per thread, one action event PER MUTATION
        # (spec/capabilities/live-activity-feed.md count-equality criterion):
        # 3 category labels + 1 archive = 4 mutations = 4 action events
        assert len(world.events_of("decision")) == 3
        assert len(world.events_of("action")) == len(world.mutations) == 4
        assert len(world.events_of("chunk_loaded")) == 1
        assert len(world.events_of("cost_tick")) == 1
        finished = world.events_of("run_finished")
        assert len(finished) == 1
        assert "3 threads" in finished[0]["sentence"]
        assert world.run_updates[-1]["status"] == "completed"
        assert world.run_updates[-1]["threads_decided"] == 3
        assert world.run_updates[-1]["llm_calls"] == 1
        assert summary["cost"]["tokens_in"] == 100

    def test_decision_persisted_before_any_mutation_for_that_thread(self):
        world = make_fake_deps(threads=[view(1)])
        run(world)
        t = world.timeline
        first_decision = t.index(("decision", "t1"))
        first_mutation = min(i for i, (kind, _) in enumerate(t) if kind == "mutation")
        assert first_decision < first_mutation

    def test_feed_sentences_are_plain_english_with_reasoning_detail(self):
        world = make_fake_deps(
            threads=[view(1, "ACME invoice #42", "billing@acme.com")],
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Finance", 0.95, "vendor invoice")}),
        )
        run(world)
        decision = world.events_of("decision")[0]
        assert "ACME invoice #42" in decision["sentence"]
        assert "Finance" in decision["sentence"]
        assert decision["detail"]["reason"] == "vendor invoice"
        action = world.events_of("action")[0]
        assert action["sentence"].startswith("Filed ")


# ------------------------------------------------------------------ edge cases


class TestEdgeCases:
    def test_empty_chunk_goes_straight_to_finalize(self):
        world = make_fake_deps(threads=[])
        summary = run(world)
        assert summary["status"] == "completed"
        assert summary["threads_decided"] == 0
        assert world.classify_batches == []
        assert world.mutations == []
        sentences = [e["sentence"] for e in world.events_of("run_finished")]
        assert any("clean" in s.lower() for s in sentences)

    def test_batching_respects_25_thread_ceiling(self):
        threads = [view(i) for i in range(30)]
        world = make_fake_deps(threads=threads, batch_size=25)
        summary = run(world)
        assert summary["threads_decided"] == 30
        assert [len(b) for b in world.classify_batches] == [25, 5]
        assert len(world.events_of("cost_tick")) == 2

    def test_chunk_limit_is_enforced(self):
        threads = [view(i) for i in range(80)]
        world = make_fake_deps(threads=threads)
        summary = run(world, chunk_limit=50)
        assert summary["threads_decided"] == 50

    def test_low_confidence_gets_best_guess_plus_needs_review_label(self):
        world = make_fake_deps(
            threads=[view(1)],
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Finance", 0.4, "maybe an invoice")}),
        )
        run(world)
        d = world.saved[0]
        assert d.category_name == "Finance" and d.needs_review is True
        muts = {(m["action"], m["label_name"]) for m in world.mutations}
        assert ("add_label", "ZI/Finance") in muts
        assert ("add_label", "ZI/Needs review") in muts
        assert "flagged for review" in world.events_of("decision")[0]["sentence"]

    def test_missing_result_becomes_needs_review_with_invalid_reason(self):
        world = make_fake_deps(
            threads=[view(1), view(2)],
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Finance", 0.9, "ok")}),
        )
        summary = run(world)
        assert summary["status"] == "completed"
        d2 = next(d for d in world.saved if d.thread_id == "t2")
        assert d2.category_name == "Needs review"
        assert d2.needs_review is True
        assert "classifier output invalid" in d2.reason

    def test_unknown_category_name_degrades_to_needs_review(self):
        world = make_fake_deps(
            threads=[view(1)],
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Spamlandia", 0.99, "??")}),
        )
        run(world)
        d = world.saved[0]
        assert d.category_name == "Needs review"
        assert "classifier output invalid" in d.reason

    def test_fallback_outcome_emits_fallback_event_and_counts_it(self):
        world = make_fake_deps(
            threads=[view(1)],
            classify_fn=lambda v, t: outcome_for(
                v, {"t1": ("Finance", 0.9, "ok")},
                was_fallback=True, provider="gemini",
                fallback_reason="NVIDIA timed out — switched to Gemini for this batch.",
            ),
        )
        summary = run(world)
        fb = world.events_of("fallback")
        assert len(fb) == 1
        assert "Gemini" in fb[0]["sentence"]
        assert summary["cost"]["fallback_events"] == 1

    def test_needs_review_category_itself_gets_no_duplicate_label(self):
        world = make_fake_deps(
            threads=[view(1)],
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Needs review", 0.3, "unsure")}),
        )
        run(world)
        labels = [m["label_name"] for m in world.mutations if m["action"] == "add_label"]
        assert labels == ["ZI/Needs review"]
