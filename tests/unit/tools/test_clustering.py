"""Clustering turns ~200 threads into ~30 decisions without losing a single thread."""

from tools.clustering import cluster
from tools.rules import DEFAULT_TAXONOMY


def make(n, *, prefix, list_id=None, from_email="a@b.com", domain="b.com", action="archive",
         category="newsletters", confidence=0.9, time_sensitive=False):
    items, decisions = [], []
    for index in range(n):
        item_id = f"{prefix}{index}"
        items.append(
            {
                "id": item_id,
                "subject": f"{prefix} subject {index}",
                "from_email": from_email,
                "from_name": prefix.title(),
                "from_domain": domain,
                "list_id": list_id,
            }
        )
        decisions.append(
            {
                "item_id": item_id,
                "category": category,
                "proposed_action": action,
                "confidence": confidence,
                "reasoning": "because",
                "decided_by": "rule",
                "time_sensitive": time_sensitive,
            }
        )
    return items, decisions


class TestGrouping:
    def test_list_id_group_wins_and_is_labelled(self):
        items, decisions = make(12, prefix="sub", list_id="<weekly.substack.com>")
        (result,) = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assert result["kind"] == "list"
        assert result["item_count"] == 12
        assert "Substack" in result["label"]
        assert result["suggested_action"] == "archive"

    def test_sender_group_used_when_no_list_id(self):
        items, decisions = make(5, prefix="rec", from_email="jobs@recruit.io", domain="recruit.io")
        (result,) = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assert result["kind"] == "sender"
        assert "jobs@recruit.io" in result["label"]

    def test_domain_group_used_when_senders_differ(self):
        items, decisions = [], []
        for index in range(6):
            new_items, new_decisions = make(
                1, prefix=f"gh{index}", from_email=f"n{index}@github.com", domain="github.com"
            )
            items += new_items
            decisions += new_decisions
        (result,) = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assert result["kind"] == "domain"
        assert result["item_count"] == 6

    def test_small_leftovers_fall_into_a_category_cluster(self):
        items, decisions = make(2, prefix="odd", from_email="one@one.io", domain="one.io")
        (result,) = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assert result["kind"] == "category"
        assert result["label"] == "Newsletters"


class TestInvariants:
    def _mixed(self):
        items, decisions = make(10, prefix="sub", list_id="<a.substack.com>")
        i2, d2 = make(4, prefix="keepme", list_id="<a.substack.com>", action="keep")
        i3, d3 = make(
            3, prefix="urgent", from_email="billing@acme.io", domain="acme.io",
            action="keep", category="urgent", time_sensitive=True,
        )
        return items + i2 + i3, decisions + d2 + d3

    def test_counts_sum_to_every_decision(self):
        items, decisions = self._mixed()
        clusters = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assert sum(c["item_count"] for c in clusters) == len(decisions)

    def test_every_decision_lands_in_exactly_one_cluster(self):
        items, decisions = self._mixed()
        clusters = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assigned = [i for c in clusters for i in c["item_ids"]]
        assert sorted(assigned) == sorted(d["item_id"] for d in decisions)

    def test_no_cluster_mixes_proposed_actions(self):
        items, decisions = self._mixed()
        action_of = {d["item_id"]: d["proposed_action"] for d in decisions}
        for result in cluster(decisions, items, categories=DEFAULT_TAXONOMY):
            assert len({action_of[i] for i in result["item_ids"]}) == 1

    def test_time_sensitive_threads_are_pulled_into_a_keep_cluster(self):
        items, decisions = self._mixed()
        clusters = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        ts_ids = {d["item_id"] for d in decisions if d["time_sensitive"]}
        holder = next(c for c in clusters if ts_ids & set(c["item_ids"]))
        assert holder["suggested_action"] == "keep"
        assert set(holder["item_ids"]) == ts_ids

    def test_min_and_avg_confidence_reported(self):
        items, decisions = make(4, prefix="s", list_id="<a.substack.com>")
        decisions[0]["confidence"] = 0.4
        (result,) = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        assert result["min_confidence"] == 0.4
        assert 0.4 < result["avg_confidence"] < 0.9

    def test_at_most_three_real_sample_subjects(self):
        items, decisions = make(9, prefix="s", list_id="<a.substack.com>")
        (result,) = cluster(decisions, items, categories=DEFAULT_TAXONOMY)
        real = {i["subject"] for i in items}
        assert len(result["sample_subjects"]) == 3
        assert set(result["sample_subjects"]) <= real


class TestEdgeCases:
    def test_no_decisions_produces_no_clusters(self):
        assert cluster([], [], categories=DEFAULT_TAXONOMY) == []

    def test_decision_for_a_missing_item_still_clusters(self):
        decisions = [
            {
                "item_id": "ghost",
                "category": "people",
                "proposed_action": "keep",
                "confidence": 0.8,
                "reasoning": "x",
                "decided_by": "llm",
            }
        ]
        clusters = cluster(decisions, [], categories=DEFAULT_TAXONOMY)
        assert sum(c["item_count"] for c in clusters) == 1
        assert clusters[0]["sample_subjects"] == []
