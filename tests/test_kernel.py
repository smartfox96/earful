"""A session and an independent worker exercise the same ports without either SDK."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from earful.core.ledger import Ledger
from earful.scripted.case import Case, Delay, DrainOptions, Fault, check_case
from earful.scripted.clock import now_us, run_virtual
from earful.scripted.header import declarations
from earful.scripted.kernel.ports import RunPorts
from earful.scripted.kernel.run import run
from earful.scripted.model import Reply, ReplyRegister
from earful.scripted.parties.activity import Activity
from earful.scripted.parties.model import fragments
from earful.scripted.parties.stt import Recognition
from earful.scripted.parties.vad import detections
from earful.scripted.world import UserSpeech


def _header(case):
    return {
        **declarations(case, (), {}),
        "session": {
            "min_interruption_duration_us": 0,
            "false_interruption_timeout_us": None,
            "min_endpointing_delay_us": 0,
            "max_endpointing_delay_us": 0,
            "turn_detection": None,
            "extra_options": {},
        },
        "providers": {
            "vad": {"min_speech_us": 0, "min_silence_us": 0},
            "tts": {"seconds_per_letter_us": 60_000, "ttfb_us": 0},
            "sink": {"can_pause": False},
            "backend": {"mode": "plain"},
        },
    }


def _run_stub(*, worker=False, hangup=None, early=False):
    calls = []
    case = Case(
        "stub", [UserSpeech(0, 0.25, "hello", stt_delay=0.75)], lambda _: Reply(), hangup=hangup
    )

    @asynccontextmanager
    async def build(case, context):
        script = Recognition(case.speeches, chronological=True)
        reply = Activity()
        completed = asyncio.get_running_loop().create_future()

        async def deliver():
            context.origin.mark("stt", now_us())
            async for event in script.events(now_us()):
                if event.final:
                    context.ledger.append(
                        "world", "user.transcript_final", speech=event.speech, text=event.text
                    )
            with reply.working():
                await asyncio.sleep(2)
            calls.append(("reply finished", now_us()))

        async def start():
            calls.append(("start", now_us()))
            nonlocal task
            task = asyncio.create_task(deliver())
            if early:
                asyncio.get_running_loop().call_later(0.1, completed.set_result, None)
            return completed if worker else None

        async def drain():
            calls.append(("drain", now_us()))
            completed.set_result(None)

        async def disconnect():
            assert context.disconnected.is_set()
            calls.append(("disconnect", now_us()))
            task.cancel()
            completed.set_result(None)

        async def close():
            calls.append(("close", now_us()))

        def history():
            calls.append(("history", now_us()))
            return {"read_from": "stub", "items": []}

        task = None
        try:
            yield RunPorts(
                lambda: _header(case),
                start,
                script,
                (script, reply),
                drain,
                disconnect,
                history,
                close,
            )
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            calls.append(("restore", now_us()))

    result = run(
        case,
        3,
        drain=DrainOptions(0.1, 5),
        virtual=True,
        require_valid=not early,
        framework={"name": "stub", "version": "1"},
        validate=lambda case, _: check_case(case),
        build=build,
        result_of=lambda result, _: result,
    )
    return result, calls


@pytest.mark.parametrize("worker", [False, True])
def test_kernel_waits_for_party_work_before_drain_and_history(worker):
    result, calls = _run_stub(worker=worker)
    assert result.invalid == []
    assert [name for name, _ in calls] == [
        "start",
        "reply finished",
        "drain",
        "history",
        "close",
        "restore",
    ]
    assert calls[2][1] >= 3_000_000
    assert [e["kind"] for e in result.events] == [
        "run.start",
        "user.origin",
        "user.transcript_final",
        "history.final",
        "run.end",
    ]


@pytest.mark.parametrize("worker", [False, True])
def test_kernel_disconnects_on_the_declared_instant_before_final_history(worker):
    result, calls = _run_stub(worker=worker, hangup=0.5)
    assert [name for name, _ in calls] == ["start", "disconnect", "history", "close", "restore"]
    assert calls[1][1] == 500_000
    assert [(e["kind"], e["t_us"]) for e in result.events[-3:]] == [
        ("user.hangup", 500_000),
        ("history.final", 600_000),
        ("run.end", 600_000),
    ]


def test_kernel_observes_worker_completion_before_input():
    result, calls = _run_stub(worker=True, early=True)
    assert result.invalid == ["harness error: the pipeline ended before the scenario did"]
    assert [name for name, _ in calls] == ["start", "history", "close", "restore"]


def test_timed_parties_deliver_their_events_without_framework_scheduling():
    speech = UserSpeech(0, 0.3, "one two three", stt_delay=0.2)
    assert [(e.kind, e.at_us) for e in detections([speech], 50_000, 100_000)] == [
        ("start", 50_000),
        ("end", 400_000),
    ]

    async def deliver():
        script = Recognition([speech], chronological=True)
        assert not script.quiet()
        events = [(e.text, e.final, now_us()) async for e in script.events(0)]
        assert script.quiet() and script.last_us == 500_000
        reply = Reply("abcdef", ttft=0.1, duration=0.3)
        register = ReplyRegister()
        ledger = Ledger(now_us)
        ledger.append("harness", "run.start", case="model timing")
        chunks = [
            (chunk, now_us())
            async for chunk in fragments(ledger, 1, reply, [], [], {}, {}, register)
        ]
        return events, chunks

    events, chunks = run_virtual(deliver)
    assert events == [("one two", False, 400_000), ("one two three", True, 500_000)]
    assert chunks == [("abc", 600_000), ("def", 800_000)]


@pytest.mark.parametrize(
    ("fields", "first"),
    [
        ({"speeches": [UserSpeech(2, 1, "")], "backend_mode": "unknown"}, "ends at 1 s"),
        (
            {"delays": [Delay("llm.ttft", 1, 0)] * 2, "faults": [Fault("invalid", 1)]},
            "declared more than once",
        ),
        (
            {"faults": [Fault("backend.timeout", 1), Fault("backend.error", 1)], "hangup": 1},
            "how long the caller waits",
        ),
    ],
)
def test_authored_refusals_keep_the_first_error(fields, first):
    args = {"name": "invalid", "speeches": [], "policy": lambda _: Reply(), **fields}
    with pytest.raises(ValueError, match=first):
        check_case(Case(**args))
