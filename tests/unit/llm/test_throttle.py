"""Rule G — the process-wide outbound rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from llm import throttle


@pytest.fixture(autouse=True)
def _clean():
    throttle.reset()
    yield
    throttle.reset()


# --- configuration ------------------------------------------------------


def test_default_target_is_350_under_the_490_account_ceiling(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_MAX_RPM", raising=False)
    assert throttle.max_rpm() == throttle.DEFAULT_MAX_RPM == 350
    assert throttle.ACCOUNT_CEILING_RPM == 490


@pytest.mark.parametrize(
    "value,expected",
    [("60", 60), ("9999", 490), ("0", 350), ("", 350), ("not-a-number", 350), ("-5", 350)],
)
def test_max_rpm_is_clamped_to_the_account_ceiling_and_never_raises(
    monkeypatch, value, expected
):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", value)
    assert throttle.max_rpm() == expected


def test_snapshot_reports_the_documented_shape(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", "120")
    snap = throttle.snapshot()
    assert set(snap) == {"max_rpm", "available", "waiting"}
    assert snap["max_rpm"] == 120
    assert snap["waiting"] == 0


# --- pacing -------------------------------------------------------------


async def test_concurrent_callers_are_paced_and_none_is_dropped(monkeypatch):
    rpm = 490
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", str(rpm))
    throttle.reset()
    n = 40
    stamps: list[float] = []

    async def _call(_i: int) -> None:
        await throttle.acquire()
        stamps.append(time.monotonic())

    # Bounded: 40 grants at 490 rpm is ~4.9 s, far inside this ceiling.
    await asyncio.wait_for(asyncio.gather(*(_call(i) for i in range(n))), timeout=60)

    assert len(stamps) == n  # the throttle delays; it never drops or fails a request
    stamps.sort()
    min_gap = 60.0 / rpm
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= min_gap * 0.8, gaps
    # No rolling 60 s window may exceed the ceiling.
    for i, start in enumerate(stamps):
        in_window = [t for t in stamps[i:] if t - start < 60.0]
        assert len(in_window) <= rpm


async def test_the_bucket_is_shared_process_wide_across_runs(monkeypatch):
    rpm = 240  # 0.25 s apart
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", str(rpm))
    throttle.reset()

    async def _run(_run_id: str, count: int) -> None:
        for _ in range(count):
            await throttle.acquire()

    started = time.monotonic()
    await asyncio.wait_for(
        asyncio.gather(_run("run-a", 6), _run("run-b", 6)), timeout=60
    )
    elapsed = time.monotonic() - started

    # 12 grants share ONE budget: >= 11 gaps of 0.25 s. A per-run bucket would
    # halve this and blow through the account limit.
    assert elapsed >= 11 * (60.0 / rpm) * 0.8


async def test_waiting_callers_are_counted_and_never_marked_failed(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", "120")  # 0.5 s apart
    throttle.reset()
    await throttle.acquire()  # drain the single token

    task = asyncio.create_task(throttle.acquire())
    await asyncio.sleep(0.15)
    assert throttle.snapshot()["waiting"] >= 1

    await asyncio.wait_for(task, timeout=10)
    assert throttle.snapshot()["waiting"] == 0


def test_sync_acquire_paces_blocking_callers(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", "240")
    throttle.reset()
    started = time.monotonic()
    for _ in range(3):
        throttle.acquire_sync()
    assert time.monotonic() - started >= 2 * (60.0 / 240) * 0.8
