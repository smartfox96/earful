"""What each witness records: tools, backend, sink word times, TTS attribution."""
import asyncio
import contextlib
import dataclasses

import livekit.agents as lk
import pytest

from earful.core.backend import EffectBackend, current_tool
from earful.core.ledger import Ledger
from earful.core.schema import validate
from earful.livekit import tools
from earful.livekit.cases import CASES, LONG_REPLY, _barge_in_policy
from earful.livekit.clock import now_us, run_virtual
from earful.livekit.harness import Case, Intent, InvalidRun, run_case
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
    assert tools.model_text(Opaque()) is None and tools.model_text(None) is None


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
    retiring = make()
    retiring.speeches = [UserSpeech(start=0.5, end=1.5, transcript="Cancel it.", retires=("nope",))]
    with pytest.raises(ValueError, match="undeclared intents"):
        run_case(retiring)


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
    assert scenario["session"]["min_interruption_duration_us"] == 500_000
    assert scenario["session"]["extra_options"] == {"max_tool_steps": 3}
    assert len(_of(events, "user.origin")) == 1
    assert len({e["run"] for e in events}) == 1


# --------------------------------------------------------------------------- sink and TTS


def test_word_completion_times_are_exact():
    events = run_case(CASES["barge_in_mid_word"]()).ledger.events
    start = _of(events, "playout.started")[0]["t_us"]
    finished = _of(events, "playout.finished")[0]["data"]
    assert finished["words_completed_us"] == [start + 300_000 * k for k in range(1, 8)]
    # paused 2.2 s in: word 8 spans 2.1-2.4 s, so 0.1 s of it (2400 samples at 24 kHz) played
    assert finished["word_cut"] == 8 and finished["word_cut_samples"] == 2400


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
    for k, t in enumerate(done, start=1):
        at = 300_000 * k
        assert t == start + at + (gap if at > paused["data"]["played_us"] else 0)


def test_tts_attribution_is_unique_in_every_case():
    for name in CASES:
        for e in _of(run_case(CASES[name]()).ledger.events, "tts.synthesized"):
            assert e["data"]["attribution"] == "unique", (name, e["data"])


def test_attribution_never_guesses():
    register = ReplyRegister()
    register.replies = {1: ["one", "moment", "please"], 2: ["one", "moment", "please"], 3: ["all", "done"]}
    assert register.attribute("all done") == (3, "unique")
    assert register.attribute("all done") == (None, "none")  # reply 3 is already synthesized
    assert register.attribute("one moment please") == (None, "ambiguous")  # replies 1 and 2
    assert register.attribute("something else") == (None, "none")
    assert register.attribute("") == (None, "none")

    stopped_early = ReplyRegister()
    stopped_early.replies = {1: ["your", "table", "is", "booked"], 2: ["how", "can", "I", "help"]}
    assert stopped_early.attribute("your table") == (1, "prefix")
    assert stopped_early.attribute("how can I help") == (2, "unique")


def test_claims_are_recorded():
    events = run_case(CASES["book_then_interrupt"]()).ledger.events
    claims = [c for e in _of(events, "llm.response") for c in e["data"]["claims"]]
    assert claims == [{"intent": "book-table-1", "first_word": 0, "last_word": 3}]
