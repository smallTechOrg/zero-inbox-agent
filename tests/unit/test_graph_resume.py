"""triage-graph slice — never-redo, resumability and the privacy seam."""

from __future__ import annotations

import json

from graph.state import Decision

from tests.unit.test_graph_nodes import make_fake_deps, outcome_for, run, view

SENTINEL = "XKCD-BODY-SENTINEL-9271"


class TestNeverRedo:
    def test_already_decided_threads_are_dropped_from_the_chunk(self):
        threads = [view(1), view(2), view(3)]
        world = make_fake_deps(threads=threads, decided={"t1", "t3"})
        summary = run(world)
        assert summary["threads_decided"] == 1
        assert [v.thread_id for v in world.classify_batches[0]] == ["t2"]
        assert {d.thread_id for d in world.saved} == {"t2"}

    def test_all_decided_means_clean_chunk_no_llm_call(self):
        world = make_fake_deps(threads=[view(1)], decided={"t1"})
        summary = run(world)
        assert summary["status"] == "completed"
        assert world.classify_batches == []


class TestResumeUnappliedDecisions:
    def pending(self):
        return [Decision(thread_id="t9", category_id="c-news",
                         category_name="Newsletters", confidence=0.92,
                         reason="bulk newsletter", needs_review=False,
                         subject="Old digest", sender="news@letters.com")]

    def test_decided_but_unapplied_decisions_are_reapplied_without_llm(self):
        world = make_fake_deps(threads=[], decided={"t9"}, pending=self.pending())
        summary = run(world)
        assert summary["status"] == "completed"
        assert world.classify_batches == []  # no re-deciding
        assert world.saved == []  # no duplicate decision rows
        muts = {(m["thread_id"], m["action"], m["label_name"]) for m in world.mutations}
        assert ("t9", "add_label", "ZI/Newsletters") in muts
        assert ("t9", "remove_inbox", None) in muts
        # one action event per applied mutation (live-activity-feed criterion)
        assert len(world.events_of("action")) == len(world.mutations) == 2

    def test_already_audited_mutation_is_not_duplicated(self):
        world = make_fake_deps(
            threads=[], decided={"t9"}, pending=self.pending(),
            existing_mutations={("t9", "add_label", "ZI/Newsletters")},
        )
        summary = run(world)
        assert summary["status"] == "completed"
        # only the missing half of the rule was applied
        assert [(m["thread_id"], m["action"]) for m in world.mutations] == [("t9", "remove_inbox")]

    def test_pending_plus_new_threads_both_processed(self):
        world = make_fake_deps(
            threads=[view(1)], decided={"t9"}, pending=self.pending(),
            classify_fn=lambda v, t: outcome_for(v, {"t1": ("Finance", 0.9, "ok")}),
        )
        summary = run(world)
        assert summary["threads_decided"] == 2  # 1 recovered + 1 new
        threads_mutated = {m["thread_id"] for m in world.mutations}
        assert threads_mutated == {"t9", "t1"}


class TestPrivacySeam:
    def test_body_from_upstream_mapping_never_reaches_the_classifier(self):
        """Even if the Gmail layer leaked a body field, the graph drops it."""
        raw = [{"thread_id": "t1", "subject": "Hello", "sender_address": "a@b.com",
                "body": SENTINEL, "snippet": "hi there"}]
        world = make_fake_deps(threads=[], fetch_fn=lambda user_id, limit: raw)
        run(world)
        assert len(world.classify_batches) == 1
        serialized = json.dumps([v.to_prompt_dict() for v in world.classify_batches[0]])
        assert SENTINEL not in serialized
        assert "body" not in json.loads(serialized)[0]
