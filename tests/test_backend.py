"""The backend a run writes to: what commits, what its caller is told, and what is recorded."""

import asyncio
import datetime

import pytest

from earful.core import backend as backends
from earful.core.backend import BackendError, BackendTimeout, EffectBackend, current_tool, effect
from earful.core.ledger import Ledger
from earful.core.schema import validate
from earful.scripted.clock import now_us, run_virtual


def test_keyed_backend_deduplicates():
    async def main():
        ledger = Ledger(lambda: 0, run="test")
        backend = EffectBackend(ledger, mode="keyed")
        token = current_tool.set(4)
        receipts = [
            await backend.write("book", {"n": 1}, key="k1"),
            await backend.write("book", {"n": 1}, key="k1"),
            await backend.write("book", {"n": 1}, key="k2"),
            await backend.write("book", {"n": 1}),
        ]
        current_tool.reset(token)
        return ledger.events, receipts, backend.commits("book")

    events, receipts, commits = asyncio.run(main())
    assert receipts == ["R1", "R1", "R2", "R3"]
    assert commits == 3
    assert [e["kind"] for e in events] == [
        "effect.requested",
        "effect.committed",
        "effect.requested",
        "effect.deduplicated",
        "effect.requested",
        "effect.committed",
        "effect.requested",
        "effect.committed",
    ]
    assert {e["data"]["tool"] for e in events if e["kind"] == "effect.requested"} == {4}


def test_keyed_backend_deduplicates_writes_that_overlap():
    async def main():
        ledger = Ledger(now_us, run="test")
        backend = EffectBackend(ledger, mode="keyed")
        receipts = await asyncio.gather(
            backend.write("book", {"n": 1}, key="k1", latency=0.4),
            backend.write("book", {"n": 1}, key="k1", latency=0.2),
        )
        return ledger.events, receipts, backend.commits("book")

    events, receipts, commits = run_virtual(main)
    assert [(e["kind"], e["t_us"], e["data"]["request"]) for e in events] == [
        ("effect.requested", 0, 1),
        ("effect.requested", 0, 2),
        ("effect.committed", 200_000, 2),
        ("effect.deduplicated", 400_000, 1),
    ]
    assert receipts == ["R1", "R1"] and commits == 1


def _faulty(faults, latencies_us=None, mode="plain"):
    """Run one write against a backend with ``faults``; what the ledger and the caller saw."""

    async def main():
        ledger = Ledger(now_us, run="test")
        backend = EffectBackend(ledger, mode=mode, faults=faults, latencies_us=latencies_us)
        try:
            said = await backend.write("book", {"n": 1}, key="k1")
        except (BackendError, BackendTimeout) as error:
            said = type(error).__name__
        await asyncio.sleep(10)  # long enough for a write still under way to finish
        return (
            [(e["kind"], e["t_us"]) for e in ledger.events],
            said,
            backend.commits("book"),
            validate_effects(ledger.events),
        )

    return run_virtual(main)


def validate_effects(events):
    """The validator's rules for effect events only: these ledgers have no header."""
    return [error for error in validate(events) if "effect" in error or "request" in error]


def test_a_rejected_write_commits_nothing_and_the_caller_is_told():
    events, said, commits, _ = _faulty({1: ("backend.error", None)}, latencies_us={1: 300_000})
    assert events == [("effect.requested", 0), ("effect.failed", 300_000)]
    assert said == "BackendError" and commits == 0


def test_a_caller_that_times_out_first_is_told_so_and_the_write_still_commits():
    events, said, commits, _ = _faulty(
        {1: ("backend.timeout", 500_000)}, latencies_us={1: 2_000_000}
    )
    assert events == [
        ("effect.requested", 0),
        ("effect.timed_out", 500_000),
        ("effect.committed", 2_000_000),
    ]
    assert said == "BackendTimeout" and commits == 1


def test_a_write_that_commits_first_is_still_reported_as_timed_out():
    events, said, commits, _ = _faulty(
        {1: ("backend.timeout", 2_000_000)}, latencies_us={1: 500_000}
    )
    assert events == [
        ("effect.requested", 0),
        ("effect.committed", 500_000),
        ("effect.timed_out", 2_000_000),
    ]
    assert said == "BackendTimeout" and commits == 1


def test_a_dropped_write_commits_nothing_and_the_caller_times_out():
    events, said, commits, _ = _faulty({1: ("backend.drop", 500_000)})
    assert events == [("effect.requested", 0), ("effect.timed_out", 500_000)]
    assert said == "BackendTimeout" and commits == 0


def test_a_fault_belongs_to_one_write_and_the_next_is_untouched():
    async def main():
        ledger = Ledger(now_us, run="test")
        backend = EffectBackend(ledger, faults={1: ("backend.error", None)})
        with pytest.raises(BackendError):
            await backend.write("book", {})
        return await backend.write("book", {}), backend.commits("book")

    assert run_virtual(main) == ("R1", 1)


def test_a_write_under_way_is_listed_until_it_has_its_outcome():
    async def main():
        ledger = Ledger(now_us, run="test")
        backend = EffectBackend(
            ledger, faults={1: ("backend.timeout", 100_000)}, latencies_us={1: 60_000_000}
        )
        with pytest.raises(BackendTimeout):
            await backend.write("book", {})
        under_way = backend.in_flight()
        await backend.settle_in_flight()
        return under_way, backend.in_flight(), [e["kind"] for e in ledger.events]

    assert run_virtual(main) == (
        [1],
        [],
        ["effect.requested", "effect.timed_out", "effect.committed"],
    )


def _abandoned(faults=None):
    async def main():
        ledger = Ledger(now_us, run="test")
        backend = EffectBackend(ledger, faults=faults, latencies_us={1: 2_000_000})
        caller = asyncio.ensure_future(backend.write("book", {}))
        await asyncio.sleep(0.5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await backend.settle_in_flight()
        return [(e["kind"], e["t_us"]) for e in ledger.events], backend.commits("book")

    return run_virtual(main)


def test_a_write_commits_although_its_caller_was_cancelled():
    """Once requested, a write is the backend's: a tool cancelled while waiting has written."""
    assert _abandoned() == ([("effect.requested", 0), ("effect.committed", 2_000_000)], 1)


def test_a_rejected_write_whose_caller_has_gone_is_still_recorded():
    events, commits = _abandoned({1: ("backend.error", None)})
    assert events == [("effect.requested", 0), ("effect.failed", 2_000_000)] and commits == 0


def test_plain_backend_commits_every_write():
    async def main():
        ledger = Ledger(lambda: 0, run="test")
        backend = EffectBackend(ledger)
        await backend.write("book", {}, key="k1")
        await backend.write("book", {}, key="k1")
        return backend.commits("book")

    assert asyncio.run(main()) == 2


def test_a_recorded_event_cannot_be_changed_by_its_caller():
    async def main():
        ledger = Ledger(lambda: 0, run="test")
        payload = {"guests": [1, 2]}
        await EffectBackend(ledger).write("book", payload)
        payload["guests"].append(3)
        payload["late"] = True
        return ledger.events[0]["data"]["payload"]

    assert asyncio.run(main()) == {"guests": [1, 2]}


def test_data_a_ledger_file_cannot_hold_is_refused_when_recorded():
    ledger = Ledger(lambda: 0, run="test")
    with pytest.raises(TypeError, match="plain JSON"):
        ledger.append("world", "tool.returned", output=object())
    with pytest.raises(TypeError, match="plain JSON"):
        ledger.append("world", "tool.returned", output=float("nan"))
    with pytest.raises(ValueError, match="run.start"):
        Ledger(lambda: 0).append("world", "user.origin", by="vad")


def test_outside_a_run_there_is_no_backend_to_write_to():
    assert backends.active() is None
    with pytest.raises(RuntimeError, match="outside an Earful run"):
        asyncio.run(effect("order-1", {"dish": "soup"}))


def test_a_write_refused_for_its_payload_is_no_request_and_the_next_one_is_write_1():
    async def main():
        ledger = Ledger(now_us, run="run-0000000000000000")
        token = backends.install(EffectBackend(ledger, faults={1: ("backend.error", None)}))
        try:
            with pytest.raises(TypeError):
                await effect("book", {"when": datetime.datetime(2026, 1, 1)})
            for invalid in ({"payload": []}, {"payload": {}, "key": []}):
                with pytest.raises(TypeError):
                    await effect("book", **invalid)
            with pytest.raises(ValueError, match="latency"):
                await effect("book", {}, latency=-1)
            assert not ledger.events
            with pytest.raises(BackendError):
                await effect("book", {"n": 1})
            assert {e["data"]["request"] for e in ledger.events} == {1}
        finally:
            backends.uninstall(token)

    run_virtual(main)
