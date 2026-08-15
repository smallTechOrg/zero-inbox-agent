"""Concurrent tier-3 batches must not kill the run on the `error` channel.

`TestFullResume` (220 threads) died with:

    langgraph.errors.InvalidUpdateError: At key 'error':
    Can receive only one value per step.

`error` was an unreduced LastValue channel, but `llm_classify_batch` fans out
via `Send` and EVERY branch returns an `error` key — `None` on success, a string
on failure. Two branches finishing in the same superstep is therefore enough to
abort the whole run. Fixture-scale runs produce one batch and one write per
step, so this only ever appeared at real inbox scale — on the exact
"resume from where it stopped" path the user asked for by name.
"""

from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph

from graph.state import TriageState, keep_first_error


def test_a_concurrent_success_never_erases_a_sibling_error():
    """The dangerous direction: a fatal batch must not be swallowed by a
    sibling that happened to succeed in the same step."""
    assert keep_first_error("boom", None) == "boom"
    assert keep_first_error(None, "boom") == "boom"


def test_the_first_error_wins_over_later_ones():
    """The reported cause should be what actually stopped the run."""
    assert keep_first_error("first", "second") == "first"


def test_clean_steps_stay_clean():
    assert keep_first_error(None, None) is None


def test_a_real_fan_out_writing_error_in_one_superstep_does_not_crash():
    """The actual regression, reproduced against LangGraph rather than asserted
    on the reducer alone: several branches write `error` in a single step."""
    from langgraph.types import Send

    def fan(_state):
        return [Send("branch", {"n": i}) for i in range(4)]

    def branch(state):
        # Mirrors llm_classify_batch: every branch returns the key, one fails.
        n = state.get("n", 0)
        return {"error": "provider circuit open" if n == 2 else None}

    def done(state):
        return {"status": "finished"}

    g = StateGraph(TriageState)
    g.add_node("branch", branch)
    g.add_node("done", done)
    g.add_conditional_edges(START, fan, ["branch"])
    g.add_edge("branch", "done")
    g.add_edge("done", END)

    final = g.compile().invoke({"run_id": "r", "user_id": "u", "channel_account_id": "c"})

    # Before the reducer this raised InvalidUpdateError instead of returning.
    assert final["error"] == "provider circuit open", (
        "the failing branch's error must survive three concurrent successes"
    )
    assert final["status"] == "finished"


def test_the_error_channel_is_actually_annotated():
    """Guard the fix itself: dropping the Annotated wrapper restores the crash,
    and nothing else in the suite would notice at fixture scale."""
    import typing

    hints = typing.get_type_hints(TriageState, include_extras=True)
    meta = getattr(hints["error"], "__metadata__", ())
    assert keep_first_error in meta, "error must keep its reducer"
