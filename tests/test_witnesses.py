"""What each witness records: tools, backend, sink word times, TTS attribution."""
import asyncio
import contextlib
import dataclasses

import livekit.agents as lk
import pytest

from earful.core.backend import BackendError, BackendTimeout, EffectBackend, current_tool
from earful.core.checker import PASS, check
from earful.core.ledger import Ledger
from earful.core.schema import validate
from earful.livekit import tools
from earful.livekit.cases import CASES, LONG_REPLY, _barge_in_policy
from earful.livekit.clock import now_us, run_virtual
from earful.livekit.harness import Case, Delay, Fault, Intent, InvalidRun, run_case
from earful.livekit.providers import Claim, Reply, ReplyRegister, ToolCall, UserSpeech
from earful.livekit.tools import witnessed


def _of(events, kind):
    return [e for e in events if e["kind"] == kind]


def _tool_policy(name, arguments, *, done="Done now."):
    def policy(items):
        if any(i["type"] == "function_call_output" for i in items):
            return Reply(text=done)
        return Reply(tool_calls=(ToolCall(name, arguments, call_id="c1"),))

    return policy


def _one_turn(name, agent, policy, **kwargs):
    return Case(
        name=name,
        agent=agent,
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="Please do it.")],
        policy=policy,
        **kwargs,
    )


# --------------------------------------------------------------------------- tools


def test_an_agent_needs_no_changes_for_its_tools_to_be_witnessed():
    """BookingAgent's tool is a plain ``@function_tool``; the harness wraps it."""
    events = run_case(CASES["book_then_interrupt"]()).ledger.events
    model_ids = [t["call_id"] for e in _of(events, "llm.response") for t in e["data"]["tool_calls"]]
    called = _of(events, "tool.called")
    assert model_ids and [e["data"]["call_id"] for e in called] == model_ids
    assert [e["data"]["arguments"] for e in called] == [{"party_size": 6}] * len(called)
    assert [e["data"]["tool"] for e in _of(events, "tool.returned")] == [e["data"]["tool"] for e in called]
    # each backend write names the tool invocation that made it
    assert [e["data"]["tool"] for e in _of(events, "effect.requested")] == [e["data"]["tool"] for e in called]


class _ContextAgent(lk.Agent):
    """A tool that asks for the framework's RunContext itself and returns a structure."""

    def __init__(self, backend):
        super().__init__(instructions="test")

    @lk.function_tool
    async def lookup(self, ctx: lk.RunContext, code: str) -> dict:
        """Look something up.

        Args:
            code: What to look up
        """
        return {"code": code, "ok": True, "seats": None}


def test_a_tool_that_declares_its_own_run_context_keeps_its_call_id():
    events = run_case(
        _one_turn("declared_context", _ContextAgent, _tool_policy("lookup", '{"code": "X1"}'))
    ).ledger.events
    (called,) = _of(events, "tool.called")
    assert called["data"]["call_id"] == "c1"
    assert called["data"]["arguments"] == {"code": "X1"}  # the context is not an argument


def test_a_structured_result_is_recorded_as_a_value_and_as_the_text_the_model_is_shown():
    events = run_case(
        _one_turn("structured", _ContextAgent, _tool_policy("lookup", '{"code": "X1"}'))
    ).ledger.events
    (returned,) = _of(events, "tool.returned")
    assert returned["data"]["output"] == {"code": "X1", "ok": True, "seats": None}
    shown = [
        item["output"]
        for e in _of(events, "llm.request")
        for item in e["data"]["items"]
        if item["type"] == "function_call_output"
    ]
    # Earful computed the text from the value the tool returned; the model was shown the same
    assert shown == [returned["data"]["text"]]


class _DecoratedAgent(lk.Agent):
    def __init__(self, backend):
        super().__init__(instructions="test")

    @lk.function_tool
    @witnessed
    async def lookup(self, code: str) -> str:
        """Look something up.

        Args:
            code: What to look up
        """
        raise lk.ToolError(f"no such code {code}")


def test_a_decorated_tool_is_witnessed_once_and_its_error_recorded():
    events = run_case(
        _one_turn("raising_tool", _DecoratedAgent, _tool_policy("lookup", '{"code": "X1"}', done="That failed."))
    ).ledger.events
    assert validate(events) == []
    assert len(_of(events, "tool.called")) == 1  # not wrapped a second time by the harness
    (raised,) = _of(events, "tool.raised")
    assert raised["data"]["error"] == "ToolError"
    assert raised["data"]["message"] == "no such code X1"
    assert _of(events, "tool.returned") == []


def test_witnessed_tool_outside_a_run_passes_through():
    agent = _DecoratedAgent(None)
    (tool,) = agent.tools
    with pytest.raises(lk.ToolError):
        asyncio.run(tool(code="Z"))


def test_a_cancelled_tool_is_recorded_as_cancelled():
    @witnessed
    async def slow(seconds: int) -> str:
        await asyncio.sleep(seconds)
        return "done"

    async def main():
        ledger = Ledger(now_us, run="test")
        token = tools.install(tools.ToolWitness(ledger))
        try:
            task = asyncio.ensure_future(slow(30))
            await asyncio.sleep(1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            tools.uninstall(token)
        return ledger.events

    events = run_virtual(main)
    assert [e["kind"] for e in events] == ["tool.called", "tool.cancelled"]
    assert events[0]["data"] == {"tool": 1, "name": "slow", "call_id": None, "arguments": {"seconds": 30}}
    assert events[1]["t_us"] == 1_000_000


def test_values_json_cannot_hold_are_named_not_printed():
    @dataclasses.dataclass
    class Seat:
        row: int

    class Opaque:
        pass

    assert tools.json_value({"seat": Seat(3), "tags": ("a", "b")}) == {"seat": {"row": 3}, "tags": ["a", "b"]}
    assert tools.json_value(Opaque()) == {"unrepresentable": "Opaque"}  # no memory address
    assert tools.model_text("ok") == "ok" and tools.model_text({"a": 1}) == "{'a': 1}"
    assert tools.model_text(Opaque()) is None
    assert tools.model_text(None) == ""  # a tool that returns nothing is shown as empty text


# --------------------------------------------------------------------------- backend


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
        "effect.requested", "effect.committed",
        "effect.requested", "effect.deduplicated",
        "effect.requested", "effect.committed",
        "effect.requested", "effect.committed",
    ]
    assert {e["data"]["tool"] for e in events if e["kind"] == "effect.requested"} == {4}


def test_keyed_backend_deduplicates_writes_that_overlap():
    """Two writes with one key, both in flight at once: both are requested before either
    is durable, one commits and the other is deduplicated against it."""

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
        await backend.cancel_in_flight()
        return [(e["kind"], e["t_us"]) for e in ledger.events], said, backend.commits("book"), validate_effects(ledger.events)

    return run_virtual(main)


def validate_effects(events):
    """The validator's rules for effect events only: these ledgers have no header."""
    return [error for error in validate(events) if "effect" in error or "request" in error]


def test_a_rejected_write_commits_nothing_and_the_caller_is_told():
    events, said, commits, _ = _faulty({1: ("backend.error", None)}, latencies_us={1: 300_000})
    assert events == [("effect.requested", 0), ("effect.failed", 300_000)]
    assert said == "BackendError" and commits == 0


def test_a_caller_that_times_out_first_is_told_so_and_the_write_still_commits():
    events, said, commits, _ = _faulty({1: ("backend.timeout", 500_000)}, latencies_us={1: 2_000_000})
    assert events == [("effect.requested", 0), ("effect.timed_out", 500_000), ("effect.committed", 2_000_000)]
    assert said == "BackendTimeout" and commits == 1


def test_a_write_that_commits_first_is_still_reported_as_timed_out():
    events, said, commits, _ = _faulty({1: ("backend.timeout", 2_000_000)}, latencies_us={1: 500_000})
    assert events == [("effect.requested", 0), ("effect.committed", 500_000), ("effect.timed_out", 2_000_000)]
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


def test_a_write_still_under_way_when_the_scenario_ends_has_no_outcome():
    async def main():
        ledger = Ledger(now_us, run="test")
        backend = EffectBackend(ledger, faults={1: ("backend.timeout", 100_000)}, latencies_us={1: 60_000_000})
        with pytest.raises(BackendTimeout):
            await backend.write("book", {})
        await backend.cancel_in_flight()
        return [e["kind"] for e in ledger.events], backend.commits("book")

    assert run_virtual(main) == (["effect.requested", "effect.timed_out"], 0)


def _abandoned(faults=None):
    """One write that takes 2 s, whose caller is cancelled after 0.5 s."""

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
    """Once requested, a write is the backend's. A tool cancelled while it waits has
    still written, as a request a real service has received is still carried out."""
    assert _abandoned() == ([("effect.requested", 0), ("effect.committed", 2_000_000)], 1)


def test_a_rejected_write_whose_caller_has_gone_is_still_recorded():
    events, commits = _abandoned({1: ("backend.error", None)})
    assert events == [("effect.requested", 0), ("effect.failed", 2_000_000)] and commits == 0


def test_the_user_hangs_up_and_a_write_under_way_still_commits():
    """The user leaves at 5 s, mid-reply, with a 6 s write requested at about 3 s. The
    output stops as they leave, the write commits at its own latency, and the final
    history is read after both."""
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]
    case.delays = [Delay("backend.latency", 1, 6.0)]
    case.hangup = 5.0
    case.drain_delay = 1.0  # less than the close takes on the releases that linger over it
    events = run_case(case).ledger.events
    (origin,), (left,) = _of(events, "user.origin"), _of(events, "user.hangup")
    assert events[0]["data"]["scenario"]["hangup_us"] == 5_000_000
    assert left["source"] == "world" and left["t_us"] == origin["t_us"] + 5_000_000
    (finished,) = _of(events, "playout.finished")
    assert finished["t_us"] == left["t_us"] and finished["data"]["interrupted"]
    (requested,), (committed,) = _of(events, "effect.requested"), _of(events, "effect.committed")
    assert requested["t_us"] < left["t_us"] < committed["t_us"] == requested["t_us"] + 6_000_000
    assert not _of(events, "tool.cancelled")  # the framework lets the tool finish
    (closed,) = _of(events, "fw.close")
    assert _of(events, "history.final")[0]["seq"] > max(committed["seq"], closed["seq"])
    assert check(events).valid


def test_a_close_that_raises_at_the_hangup_is_the_runs_error(monkeypatch):
    """The harness's own handling, so the framework is patched here: the first close raises."""
    close, calls = lk.AgentSession.aclose, []

    async def raises_once(session):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the close failed")
        await close(session)

    monkeypatch.setattr(lk.AgentSession, "aclose", raises_once)
    case = CASES["barge_in_mid_word"]()
    case.speeches, case.hangup = case.speeches[:1], 5.0
    with pytest.raises(RuntimeError, match="the close failed"):
        run_case(case)


def test_a_fault_that_is_no_fault_is_refused():
    def make(*faults):
        case = CASES["book_then_interrupt"]()
        case.faults = list(faults)
        return case

    for bad, message in (
        ((Fault("backend.explode", 1),), "unknown fault"),
        ((Fault("backend.error", 0),), "counts from 1"),
        ((Fault("backend.timeout", 1),), "how long the caller waits"),
        ((Fault("backend.error", 1, 0.5),), "an error does not"),
        ((Fault("backend.error", 1), Fault("backend.drop", 1, 0.5)), "more than one fault"),
    ):
        with pytest.raises(ValueError, match=message):
            run_case(make(*bad))


def test_declared_faults_are_in_the_header_and_the_run_is_valid():
    case = CASES["book_then_interrupt"]()
    case.speeches = case.speeches[:1]  # nothing interrupts the reply that made the call
    case.faults = [Fault("backend.timeout", 1, 0.5)]
    case.delays = [Delay("backend.latency", 1, 2.0)]
    events = run_case(case).ledger.events
    assert events[0]["data"]["scenario"]["faults"] == [{"of": "backend.timeout", "nth": 1, "us": 500_000}]
    assert validate(events) == []
    gave_up, committed = _of(events, "effect.timed_out")[0], _of(events, "effect.committed")[0]
    assert gave_up["data"]["after_us"] == 500_000 and committed["t_us"] - gave_up["t_us"] == 1_500_000
    # The tool was told; the model was shown an error for that call.
    raised = _of(events, "tool.raised")[0]
    assert raised["t_us"] == gave_up["t_us"]
    shown = [i for i in _of(events, "llm.request")[1]["data"]["items"] if i["type"] == "function_call_output"]
    assert [i["is_error"] for i in shown] == [True]


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


class _TypoAgent(lk.Agent):
    def __init__(self, backend):
        super().__init__(instructions="test")
        self._backend = backend

    @lk.function_tool
    async def book(self) -> str:
        """Book a table."""
        return await self._backend.write("book-tabel", {})


def test_a_write_for_an_undeclared_intent_makes_the_run_invalid():
    """A misspelt intent must not quietly leave a contract with nothing to check."""
    case = _one_turn("typo", _TypoAgent, _tool_policy("book", "{}"), intents=[Intent("book-table")])
    with pytest.raises(InvalidRun, match="undeclared intent 'book-tabel'") as caught:
        run_case(case)
    assert _of(caught.value.result.ledger.events, "effect.committed")  # the evidence is kept


# --------------------------------------------------------------------------- scenario


def test_a_policy_that_reuses_a_tool_call_id_is_a_harness_error():
    """A call id links an invocation to its result; a model never gives two calls one id."""
    turns = {"n": 0}

    def policy(items):
        outputs = sum(1 for i in items if i["type"] == "function_call_output")
        users = sum(1 for i in items if i["type"] == "message" and i.get("role") == "user")
        if outputs < users:
            return Reply(tool_calls=(ToolCall("lookup", '{"code": "X1"}', call_id="same"),))
        turns["n"] += 1
        return Reply(text=f"Done {turns['n']}.")

    case = _one_turn("reuse", _ContextAgent, policy)
    case.speeches = [UserSpeech(0.5, 1.5, "Look it up."), UserSpeech(5.0, 6.0, "And again.")]
    with pytest.raises(InvalidRun, match="tool call id 'same' is used twice") as caught:
        run_case(case)
    assert any("reused tool call id" in e for e in caught.value.result.harness_errors)


def test_a_claim_that_does_not_fit_its_reply_is_a_harness_error():
    def policy(items):
        return Reply(text="All booked.", claims=(Claim("book-table", first_word=0, last_word=5),))

    case = _one_turn("bad_claim", lambda backend: lk.Agent(instructions="t"), policy, intents=[Intent("book-table")])
    with pytest.raises(InvalidRun, match="does not fit reply"):
        run_case(case)


def test_a_scenario_that_contradicts_itself_is_refused_before_it_runs():
    def make(**kwargs):
        return _one_turn("bad", lambda backend: lk.Agent(instructions="t"), _barge_in_policy, **kwargs)

    with pytest.raises(ValueError, match="use the Case fields"):
        run_case(make(session_kwargs={"min_interruption_duration": 0.1}))
    with pytest.raises(ValueError, match="unknown contracts"):
        run_case(make(contracts={"M9": {}}))
    overlapping = make()
    overlapping.speeches = [UserSpeech(0.5, 1.5, "Please do it."), UserSpeech(1.4, 2.5, "Right now.")]
    with pytest.raises(ValueError, match="overlap"):
        run_case(overlapping)
    retiring = make()
    retiring.speeches = [UserSpeech(start=0.5, end=1.5, transcript="Cancel it.", retires=("nope",))]
    with pytest.raises(ValueError, match="undeclared intents"):
        run_case(retiring)
    leaving = make()
    leaving.speeches = [UserSpeech(0.5, 1.5, "Please do it.")]
    leaving.hangup = 1.0
    with pytest.raises(ValueError, match="hangs up at 1.0 s, before the speech at 0.5 s is over"):
        run_case(leaving)
    leaving.speeches = []
    with pytest.raises(ValueError, match="never speaks"):
        run_case(leaving)


def test_speech_that_resumes_within_the_vads_silence_window_is_one_segment_to_it():
    """The user pauses for 0.3 s mid-sentence. A VAD reports the end of speech only after
    0.5 s of silence, so it reports one segment: its start with the first speech, its end
    after the second. The recognizer still delivers two transcripts."""
    case = CASES["barge_in_mid_word"]()
    case.speeches = [UserSpeech(0.5, 1.5, "Tell me about"), UserSpeech(1.8, 2.8, "the menu please.")]
    events = run_case(case).ledger.events
    reports = [(e["kind"], e["data"]["speech"], e["t_us"]) for e in events if e["kind"].startswith("user.vad")]
    assert reports == [("user.vad_start", 0, 550_000), ("user.vad_end", 1, 3_300_000)]
    assert [e["data"]["text"] for e in _of(events, "user.transcript_final")] == ["Tell me about", "the menu please."]
    report = check(events)
    assert report.failures == () and report.outcome("L2") == PASS
    assert [v.instance for v in report.results["L2"].verdicts] == [{"speech": 0}, {"speech": 1}]
    resumed = report.results["L2"].verdicts[1].measures
    assert resumed["floor_opened_us"] == 1_800_000  # its own start: the VAD had already reported
    assert resumed["interruption_point_us"] == 1_800_000  # 0.5 s of the segment had passed


def test_speech_a_full_silence_window_later_is_a_segment_of_its_own():
    case = CASES["barge_in_mid_word"]()
    case.speeches = [UserSpeech(0.5, 1.5, "Tell me about"), UserSpeech(2.0, 3.0, "the menu please.")]
    events = run_case(case).ledger.events
    assert [(e["kind"], e["data"]["speech"]) for e in events if e["kind"].startswith("user.vad")] == [
        ("user.vad_start", 0), ("user.vad_end", 0), ("user.vad_start", 1), ("user.vad_end", 1),
    ]


def test_header_declares_the_scenario_and_what_ran_it():
    case = CASES["book_then_interrupt"]()
    case.session_kwargs = {"max_tool_steps": 3}
    events = run_case(case).ledger.events
    header = events[0]["data"]
    assert header["framework"] == {"name": "livekit-agents", "version": lk.__version__}
    assert header["clock"] == "virtual"
    scenario = header["scenario"]
    assert [s["start_us"] for s in scenario["speeches"]] == [500_000, 5_000_000]
    assert all(s["expects_reply"] for s in scenario["speeches"])
    assert scenario["intents"] == [
        {"intent": "book-table-1", "tools": ["create_reservation"], "authorized": 1}
    ]
    assert scenario["witnessed_tools"] == ["create_reservation"]
    assert scenario["session"]["min_interruption_duration_us"] == 500_000
    assert scenario["session"]["extra_options"] == {"max_tool_steps": 3}
    assert len(_of(events, "user.origin")) == 1
    assert len({e["run"] for e in events}) == 1


# --------------------------------------------------------------------------- sink and TTS


def _word_ends_us(events, utterance=0):
    """When each word of a synthesized text ends, in microseconds of audio."""
    tts = _of(events, "tts.synthesized")[utterance]["data"]
    ends, total = [], 0
    for samples in tts["word_samples"]:
        total += samples
        ends.append(-(-total * 1_000_000 // tts["sample_rate"]))
    return ends


def test_a_word_is_as_long_as_its_letters():
    events = run_case(CASES["barge_in_mid_word"]()).ledger.events
    tts = _of(events, "tts.synthesized")[0]["data"]
    words = tts["text"].split()
    # 0.06 s a letter at 24 kHz: "Tonight" 7 letters, "we" 2, "a" 1
    assert tts["word_samples"][:4] == [7 * 1440, 2 * 1440, 4 * 1440, 1 * 1440]
    assert len(tts["word_samples"]) == len(words) == len(tts["stamps"])


def test_word_completion_times_are_exact():
    events = run_case(CASES["barge_in_mid_word"]()).ledger.events
    start = _of(events, "playout.started")[0]["t_us"]
    finished = _of(events, "playout.finished")[0]["data"]
    ends = _word_ends_us(events)
    assert finished["words_played"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert finished["words_completed_us"] == [start + end for end in ends[:8]]
    # paused 2.2 s in: the ninth word, "start", spans 1.98-2.28 s, so 0.22 s of it played
    assert finished["word_cut"] == 9 and finished["word_cut_samples"] == 5280


def test_word_completion_times_skip_a_pause():
    """Noise pauses the reply (a false interruption); it resumes and plays to the end.
    Words after the pause complete later by exactly the length of the pause."""
    case = Case(
        name="false_interruption",
        agent=lambda backend: lk.Agent(instructions="You are a restaurant host."),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="Tell me about the menu."),
            UserSpeech(start=4.0, end=4.8, transcript=""),
        ],
        policy=_barge_in_policy,
    )
    events = run_case(case).ledger.events
    assert validate(events) == []
    start = _of(events, "playout.started")[0]["t_us"]
    paused = _of(events, "playout.paused")[0]
    resumed = _of(events, "playout.resumed")[0]
    gap = resumed["t_us"] - paused["t_us"]
    assert gap > 0
    done = _of(events, "playout.finished")[0]["data"]["words_completed_us"]
    assert len(done) == len(LONG_REPLY.split())
    for end, t in zip(_word_ends_us(events), done):
        assert t == start + end + (gap if end > paused["data"]["played_us"] else 0)


def test_tts_attribution_is_unique_in_every_case():
    for name in CASES:
        for e in _of(run_case(CASES[name]()).ledger.events, "tts.synthesized"):
            assert e["data"]["attribution"] == "unique", (name, e["data"])


def test_attribution_names_every_candidate_and_chooses_none():
    register = ReplyRegister()
    register.replies = {1: ["one", "moment", "please"], 2: ["one", "moment", "please"], 3: ["all", "done"]}
    assert register.attribute("all done") == (3, "unique", [3])
    assert register.attribute("all done") == (None, "none", [])  # reply 3 is already synthesized
    assert register.attribute("one moment please") == (None, "ambiguous", [1, 2])
    assert register.attribute("one moment please") == (None, "ambiguous", [1, 2])  # still both
    assert register.attribute("something else") == (None, "none", [])
    assert register.attribute("") == (None, "none", [])

    stopped_early = ReplyRegister()
    stopped_early.replies = {1: ["your", "table", "is", "booked"], 2: ["how", "can", "I", "help"]}
    assert stopped_early.attribute("your table") == (1, "prefix", [1])
    assert stopped_early.attribute("how can I help") == (2, "unique", [2])


def test_claims_are_recorded():
    events = run_case(CASES["book_then_interrupt"]()).ledger.events
    claims = [c for e in _of(events, "llm.response") for c in e["data"]["claims"]]
    assert claims == [{"intent": "book-table-1", "first_word": 0, "last_word": 3}]
