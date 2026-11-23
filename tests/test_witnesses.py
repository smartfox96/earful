"""What each witness records: tools, backend, sink word times, TTS attribution."""

import asyncio
import contextlib
import dataclasses
import enum

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk
from shapes.corpus import KnownDefect

from earful.core.backend import (
    effect,
)
from earful.core.checker import NOT_EXERCISED, PASS, check
from earful.core.ledger import Ledger
from earful.core.schema import validate
from earful.livekit import tools
from earful.livekit.cases import CASES, LONG_REPLY, _barge_in_policy
from earful.livekit.harness import Case, Delay, Fault, Intent, InvalidRun, run_case
from earful.livekit.providers import (
    Claim,
    Reply,
    ReplyRegister,
    ScriptedVAD,
    ToolCall,
    UserSpeech,
    WorldOrigin,
)
from earful.livekit.tools import witnessed
from earful.scripted.clock import now_us, run_virtual
from earful.scripted.playbook import ScriptedModel


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


def test_an_agent_needs_no_changes_for_its_tools_to_be_witnessed():
    """BookingAgent's tool is a plain ``@function_tool``; the harness wraps it."""
    events = run_case(CASES["book_then_interrupt"]()).ledger.events
    model_ids = [t["call_id"] for e in _of(events, "llm.response") for t in e["data"]["tool_calls"]]
    called = _of(events, "tool.called")
    assert model_ids and [e["data"]["call_id"] for e in called] == model_ids
    assert [e["data"]["arguments"] for e in called] == [{"party_size": 6}] * len(called)
    assert [e["data"]["tool"] for e in _of(events, "tool.returned")] == [
        e["data"]["tool"] for e in called
    ]
    assert [e["data"]["tool"] for e in _of(events, "effect.requested")] == [
        e["data"]["tool"] for e in called
    ]


class _ContextAgent(lk.Agent):
    """A tool that asks for the framework's RunContext itself and returns a structure."""

    def __init__(self):
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
    # the text Earful computed from the returned value is what the model was shown
    assert shown == [returned["data"]["text"]]


class _DecoratedAgent(lk.Agent):
    def __init__(self):
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
        _one_turn(
            "raising_tool",
            _DecoratedAgent,
            _tool_policy("lookup", '{"code": "X1"}', done="That failed."),
        )
    ).ledger.events
    assert validate(events) == []
    assert len(_of(events, "tool.called")) == 1  # not wrapped a second time by the harness
    (raised,) = _of(events, "tool.raised")
    assert raised["data"]["error"] == "ToolError"
    assert raised["data"]["message"] == "no such code X1"
    assert _of(events, "tool.returned") == []


def test_witnessed_tool_outside_a_run_passes_through():
    agent = _DecoratedAgent()
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
    assert events[0]["data"] == {
        "tool": 1,
        "name": "slow",
        "call_id": None,
        "arguments": {"seconds": 30},
    }
    assert events[1]["t_us"] == 1_000_000


def test_values_json_cannot_hold_are_named_not_printed():
    @dataclasses.dataclass
    class Seat:
        row: int

    class Opaque:
        pass

    assert tools.json_value({"seat": Seat(3), "tags": ("a", "b")}) == {
        "seat": {"row": 3},
        "tags": ["a", "b"],
    }
    assert tools.json_value(Opaque()) == {"unrepresentable": "Opaque"}  # no memory address
    assert tools.model_text("ok") == "ok" and tools.model_text({"a": 1}) == "{'a': 1}"
    assert tools.model_text(Opaque()) is None
    assert tools.model_text(None) == ""  # a tool that returns nothing is shown as empty text


@pytest.mark.xfail(
    strict=True, raises=KnownDefect, reason="Earful: an enumeration is recorded by its type"
)
def test_an_argument_from_an_enumeration_is_recorded_by_its_value():
    class Size(enum.Enum):
        LARGE = "large"

    recorded = tools.json_value({"size": Size.LARGE})
    if recorded != {"size": "large"}:
        raise KnownDefect(f"recorded as {recorded}")


def test_an_agent_is_taken_out_of_what_a_tool_returns_and_the_rest_is_shown():
    agent, other = lk.Agent(instructions="next"), lk.Agent(instructions="another")
    assert tools.model_text(agent) == "" and tools.model_text((agent, "a")) == "a"
    assert tools.model_text([agent, "a", "b"]) == "['a', 'b']"
    assert tools.model_text({agent, "a"}) == "a"
    assert tools.model_text({agent, "a", "b"}) is None  # a set has no order to show them in
    assert tools.hands_over(agent) and tools.hands_over(("a", agent))
    assert not tools.hands_over("a") and not tools.hands_over(("a", "b"))
    assert not tools.expects_reply(agent) and not tools.expects_reply((agent, None))
    assert tools.expects_reply((agent, "")) and tools.expects_reply((agent, "a", "b"))
    # Several agents: the framework hands over to none, and each release answers its own way.
    assert tools.model_text((agent, other)) is None and not tools.hands_over((agent, other))
    assert tools.expects_reply((agent, other))


class _PairAgent(lk.Agent):
    def __init__(self):
        super().__init__(instructions="test")

    @lk.function_tool
    async def lookup(self, code: str):
        """Look up a code.

        Args:
            code: the code
        """
        return code, 4


def test_a_tuple_a_tool_returns_is_recorded_as_the_list_the_model_is_shown():
    result = run_case(_one_turn("pair", _PairAgent, _tool_policy("lookup", '{"code": "X1"}')))
    (returned,) = _of(result.ledger.events, "tool.returned")
    assert returned["data"]["output"] == ["X1", 4] and returned["data"]["text"] == "['X1', 4]"
    assert tools.model_text(("X1", 4)) == tools.model_text(["X1", 4])
    assert result.report.outcome("M2") == PASS


def test_the_user_hangs_up_and_a_write_under_way_still_commits():
    """The user leaves at 5 s, mid-reply, with a 6 s write requested at about 3 s."""
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
    assert events[0]["data"]["scenario"]["faults"] == [
        {"of": "backend.timeout", "nth": 1, "us": 500_000}
    ]
    assert validate(events) == []
    gave_up, committed = _of(events, "effect.timed_out")[0], _of(events, "effect.committed")[0]
    assert (
        gave_up["data"]["after_us"] == 500_000 and committed["t_us"] - gave_up["t_us"] == 1_500_000
    )
    raised = _of(events, "tool.raised")[0]
    assert raised["t_us"] == gave_up["t_us"]
    shown = [
        i
        for i in _of(events, "llm.request")[1]["data"]["items"]
        if i["type"] == "function_call_output"
    ]
    assert [i["is_error"] for i in shown] == [True]


class _TypoAgent(lk.Agent):
    def __init__(self):
        super().__init__(instructions="test")

    @lk.function_tool
    async def book(self) -> str:
        """Book a table."""
        return await effect("book-tabel", {})


def test_a_write_for_an_undeclared_intent_makes_the_run_invalid():
    """A misspelt intent must not quietly leave a contract with nothing to check."""
    case = _one_turn("typo", _TypoAgent, _tool_policy("book", "{}"), intents=[Intent("book-table")])
    with pytest.raises(InvalidRun, match="undeclared intent 'book-tabel'") as caught:
        run_case(case)
    assert _of(caught.value.result.ledger.events, "effect.committed")  # the evidence is kept


def test_a_policy_that_reuses_a_tool_call_id_is_a_harness_error():
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

    case = _one_turn(
        "bad_claim",
        lambda: lk.Agent(instructions="t"),
        policy,
        intents=[Intent("book-table")],
    )
    with pytest.raises(InvalidRun, match="does not fit reply"):
        run_case(case)


def test_a_scenario_that_contradicts_itself_is_refused_before_it_runs():
    def make(**kwargs):
        return _one_turn("bad", lambda: lk.Agent(instructions="t"), _barge_in_policy, **kwargs)

    with pytest.raises(ValueError, match="use Case.min_interruption_duration"):
        run_case(make(session_kwargs={"min_interruption_duration": 0.1}))
    with pytest.raises(ValueError, match="unknown contracts"):
        run_case(make(contracts={"M9": {}}))
    overlapping = make()
    overlapping.speeches = [
        UserSpeech(0.5, 1.5, "Please do it."),
        UserSpeech(1.4, 2.5, "Right now."),
    ]
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


def test_the_scripted_vad_declares_the_interval_it_reports_at():
    ledger = Ledger(lambda: 0)
    for seconds in (0.05, 0.2):
        vad = ScriptedVAD(ledger, [], WorldOrigin(ledger, []), min_speech_duration=seconds)
        assert vad.capabilities.update_interval == seconds


def test_speech_that_resumes_within_the_vads_silence_window_is_one_segment_to_it():
    """The user pauses for 0.3 s mid-sentence. A VAD reports the end of speech only after 0.5 s
    of silence, so it reports one segment; the recognizer still delivers two transcripts."""
    case = CASES["barge_in_mid_word"]()
    case.speeches = [
        UserSpeech(0.5, 1.5, "Tell me about"),
        UserSpeech(1.8, 2.8, "the menu please."),
    ]
    events = run_case(case).ledger.events
    reports = [
        (e["kind"], e["data"]["speech"], e["t_us"])
        for e in events
        if e["kind"].startswith("user.vad")
    ]
    assert reports == [("user.vad_start", 0, 550_000), ("user.vad_end", 1, 3_300_000)]
    assert [e["data"]["text"] for e in _of(events, "user.transcript_final")] == [
        "Tell me about",
        "the menu please.",
    ]
    report = check(events)
    assert report.failures == () and report.outcome("L2") == NOT_EXERCISED  # nothing to stop
    assert [v.instance for v in report.results["L2"].verdicts] == [{"speech": 0}, {"speech": 1}]
    resumed = report.results["L2"].verdicts[1].measures
    assert resumed["floor_opened_us"] == 1_800_000  # its own start: the VAD had already reported
    assert resumed["interruption_point_us"] == 1_800_000  # 0.5 s of the segment had passed


def test_speech_a_full_silence_window_later_is_a_segment_of_its_own():
    case = CASES["barge_in_mid_word"]()
    case.speeches = [
        UserSpeech(0.5, 1.5, "Tell me about"),
        UserSpeech(2.0, 3.0, "the menu please."),
    ]
    events = run_case(case).ledger.events
    assert [
        (e["kind"], e["data"]["speech"]) for e in events if e["kind"].startswith("user.vad")
    ] == [
        ("user.vad_start", 0),
        ("user.vad_end", 0),
        ("user.vad_start", 1),
        ("user.vad_end", 1),
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
    assert finished["words_played"] == list(range(1, 10))
    assert finished["words_completed_us"] == [start + end for end in ends[:9]]
    # Streaming starts 0.1 s sooner: "then" is cut 20 ms into its 240 ms waveform.
    assert finished["word_cut"] == 10 and finished["word_cut_samples"] == 480


def test_word_completion_times_skip_a_pause():
    """Noise pauses the reply (a false interruption); it resumes and plays to the end."""
    case = Case(
        name="false_interruption",
        agent=lambda: lk.Agent(instructions="You are a restaurant host."),
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
    register.replies = {
        1: ["one", "moment", "please"],
        2: ["one", "moment", "please"],
        3: ["all", "done"],
    }
    assert register.attribute("all done") == (3, "unique", [3])
    assert register.attribute("all done") == (None, "none", [])  # reply 3 is already synthesized
    assert register.attribute("one moment please") == (None, "ambiguous", [1, 2])
    assert register.attribute("one moment please") == (None, "ambiguous", [1, 2])
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


def test_an_agent_that_starts_with_history_records_it_and_is_judged_without_it():
    from livekit.agents import llm

    def agent():
        ctx = llm.ChatContext()
        ctx.add_message(role="user", content="Hi, I called earlier.")
        ctx.add_message(role="assistant", content="Welcome back.")
        return lk.Agent(instructions="You take bookings.", chat_ctx=ctx)

    case = Case(
        name="seeded",
        agent=agent,
        speeches=[UserSpeech(0.5, 1.5, "A table for two, please.")],
        policy=ScriptedModel().otherwise(say="Of course."),
    )
    result = run_case(case)
    history = result.ledger.events[0]["data"]["scenario"]["history"]
    assert [(i["role"], i["text"], i["id"]) for i in history] == [
        ("user", "Hi, I called earlier.", None),
        ("assistant", "Welcome back.", None),
    ]
    outcomes = result.report.outcomes()
    assert outcomes["M2"] == outcomes["L1"] == "pass"
    assert result.hash == run_case(case).hash
    plain = run_case(CASES["book_then_interrupt"]())
    assert "history" not in plain.ledger.events[0]["data"]["scenario"]
