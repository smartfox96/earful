"""What a run on LiveKit models of the session's and the agent's options, and what it refuses."""

import re

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk

from earful.core.checker import FAIL, PASS
from earful.livekit import options
from earful.livekit.cases import CASES
from earful.livekit.harness import Case, run_case
from earful.livekit.providers import ToolCall, UserSpeech, _unserved
from earful.scripted.playbook import ScriptedModel, tool_succeeded


def _case(agent=lambda: lk.Agent(instructions="t"), **fields):
    fields.setdefault("speeches", [UserSpeech(0.5, 1.5, "Hello.")])
    fields.setdefault("policy", ScriptedModel().otherwise(say="Hi."))
    return Case(name="options", agent=agent, **fields)


def _session(result):
    return result.ledger.events[0]["data"]["scenario"]["session"]


def test_run_calls_the_cases_own_check_with_its_seed():
    class CheckedCase(Case):
        def check(self, seed=0):
            raise ValueError(f"application refused seed {seed}")

    case = CheckedCase(**vars(_case()))
    with pytest.raises(ValueError, match="application refused seed 19"):
        run_case(case, seed=19)


def test_every_option_of_the_installed_release_is_modelled_or_refused():
    session = options.PASSED.keys() | options.FROM_CASE.keys() | options.REFUSED.keys()
    agent = options.AGENT_RUN | options.AGENT_TURNS | options.AGENT_REFUSED.keys()
    assert options.session_options() - session == set(), lk.__version__
    assert options.agent_options() - agent == set(), lk.__version__


@pytest.mark.parametrize("name", sorted(options.FROM_CASE))
def test_an_option_a_case_field_sets_is_refused_in_session_kwargs(name):
    with pytest.raises(ValueError, match=rf"may not set '{name}': use Case\."):
        _case(session_kwargs={name: 1}).check()


@pytest.mark.parametrize("name", sorted(options.REFUSED))
def test_an_option_earful_does_not_model_is_refused_with_why(name):
    said = f"session_kwargs {name!r} {options.REFUSED[name]}"
    with pytest.raises(ValueError, match=re.escape(said)):
        _case(session_kwargs={name: 1}).check()


def test_an_option_no_release_has_is_refused():
    with pytest.raises(ValueError, match="'speed' is not an AgentSession option Earful models"):
        _case(session_kwargs={"speed": 1.2}).check()


def test_an_option_the_installed_release_does_not_take_is_refused(monkeypatch):
    taken = options.session_options() - {"max_tool_steps"}
    monkeypatch.setattr(options, "session_options", lambda: taken)
    with pytest.raises(ValueError, match="'max_tool_steps' is not an AgentSession option"):
        _case(session_kwargs={"max_tool_steps": 2}).check()


def test_a_case_that_turns_interruptions_off_is_run_without_them():
    case = CASES["barge_in_mid_word"]()
    case.allow_interruptions = False
    result = run_case(case)
    assert _session(result)["extra_options"] == {"allow_interruptions": False}
    assert result.report.outcome("L2") == FAIL  # the agent talks on over "Stop."
    case.contracts = {"L2": {"interruptions_allowed": False}}
    assert run_case(case).report.outcome("L2") == PASS


def test_a_case_that_turns_preemptive_generation_off_asks_the_model_at_the_turn_end():
    def first_request(case):
        result = run_case(case)
        return next(e["t_us"] for e in result.events if e["kind"] == "llm.request"), result

    early, on = first_request(CASES["barge_in_mid_word"]())
    case = CASES["barge_in_mid_word"]()
    case.preemptive_generation = False
    late, off = first_request(case)
    assert _session(on)["extra_options"] == {}
    assert _session(off)["extra_options"] == {"preemptive_generation": False}
    assert early < late == 2_500_000 + _session(off)["turn_end_delay_us"]


@pytest.mark.parametrize("provider", ["stt", "vad", "llm", "tts"])
def test_an_agent_with_a_provider_of_its_own_is_refused(provider):
    case = _case(agent=lambda: lk.Agent(instructions="t", **{provider: None}))
    with pytest.raises(ValueError, match=f"^Agent sets its own {provider}, which would replace"):
        run_case(case)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("use_tts_aligned_transcript", True),
        ("mcp_servers", [object()]),
        ("tool_handling", {"async_options": {}}),
        ("expressive", True),
    ],
)
def test_an_agent_option_earful_does_not_model_is_refused(option, value):
    if option not in options.agent_options():
        pytest.skip(f"livekit-agents {lk.__version__} has no Agent option {option}")
    case = _case(agent=lambda: lk.Agent(instructions="t", **{option: value}))
    with pytest.raises(ValueError, match=f"^Agent sets its own {option}"):
        run_case(case)


def test_an_agent_turn_option_the_header_states_has_to_be_the_case_s():
    case = _case(agent=lambda: lk.Agent(instructions="t", min_endpointing_delay=2.0))
    said = (
        "Agent sets endpointing min_delay=2.0 and the case gives the session 0.5: "
        "set Case.min_endpointing_delay"
    )
    with pytest.raises(ValueError, match=re.escape(said)):
        run_case(case)
    case.min_endpointing_delay = 2.0
    assert _session(run_case(case))["min_endpointing_delay_us"] == 2_000_000


@pytest.mark.parametrize(
    ("turn_handling", "said"),
    [
        (
            {"endpointing": {"mode": "dynamic"}},
            "mode='dynamic', and Earful models only the session's 'fixed'",
        ),
        (
            {"interruption": {"mode": "adaptive"}},
            "mode='adaptive', and Earful models only the session's 'vad'",
        ),
        ({"interruption": {"min_duration": 0.2}}, "set Case.min_interruption_duration"),
        ({"turn_detection": "stt"}, "turn_detection='stt': Earful scripts the VAD"),
        ({"turn_detection": "manual"}, "turn_detection='manual'"),
    ],
)
def test_an_agent_turn_option_earful_does_not_model_is_refused(turn_handling, said):
    case = _case(agent=lambda: lk.Agent(instructions="t", turn_handling=turn_handling))
    with pytest.raises(ValueError, match=re.escape(said)):
        run_case(case)


@pytest.mark.parametrize("turn_detection", [None, "vad"])
def test_an_agent_that_names_the_default_turn_detection_runs_as_the_default(turn_detection):
    case = CASES["barge_in_mid_word"]()
    default = run_case(case).hash
    case.agent = lambda: lk.Agent(
        instructions="You are a restaurant host.", turn_detection=turn_detection
    )
    assert run_case(case).hash == default


def test_an_agent_sets_its_own_preemptive_generation():
    agent = lk.Agent(instructions="t", turn_handling={"preemptive_generation": {"enabled": False}})
    assert run_case(_case(agent=lambda: agent)).invalid == []


class _Books(lk.Agent):
    min_endpointing_delay = 2.0

    def __init__(self, chat_ctx):
        super().__init__(
            instructions="You book tables.",
            chat_ctx=chat_ctx,
            min_endpointing_delay=self.min_endpointing_delay,
        )


class _Greets(lk.Agent):
    then = _Books

    def __init__(self):
        super().__init__(instructions="You greet the caller.")

    @lk.function_tool
    async def transfer(self) -> lk.Agent:
        """Hand the caller to the agent that books."""
        return self.then(self.chat_ctx)


def test_an_agent_handed_over_to_is_held_to_the_same():
    model = (
        ScriptedModel()
        .when(tool_succeeded("transfer"), say="How many guests?")
        .otherwise(call=("transfer", {}))
    )
    case = _case(agent=_Greets, policy=model)
    result = run_case(case, require_valid=False)
    said = "an agent that became active during the run: _Books sets endpointing min_delay=2.0"
    assert [error for error in result.harness_errors if said in error]
    assert result.invalid

    class _BooksAsTheCaseSays(_Books):
        min_endpointing_delay = 0.5

    class _GreetsAgain(_Greets):
        then = _BooksAsTheCaseSays

    case.agent = _GreetsAgain
    assert run_case(case).harness_errors == []


@lk.function_tool
async def look_up_hours(day: str) -> str:
    """Look up the opening hours.

    Args:
        day: The day of the week
    """
    return "We open at five."


def test_a_tool_call_where_the_request_allows_none_is_a_harness_error():
    case = _case(
        agent=lambda: lk.Agent(instructions="t", tools=[look_up_hours]),
        policy=ScriptedModel().otherwise(call=("look_up_hours", {"day": "Friday"})),
        session_kwargs={"max_tool_steps": 2},
    )
    result = run_case(case, require_valid=False)
    (error,) = result.harness_errors
    assert "allowed no tool call (tool_choice='none')" in error and "max_tool_steps" in error


def _calls(*names):
    return tuple(ToolCall(name, "{}") for name in names)


_NAMED = {"type": "function", "function": {"name": "a"}}


@pytest.mark.parametrize(
    ("tool_choice", "parallel", "calls", "said"),
    [
        (None, None, (), None),
        ("auto", None, ("a", "b"), None),
        ("none", None, (), None),
        ("none", None, ("a",), "allowed no tool call"),
        ("required", None, ("a",), None),
        ("required", None, (), "required a tool call"),
        (_NAMED, None, ("a",), None),
        (_NAMED, None, ("b",), "asked for a call of 'a'"),
        (_NAMED, None, (), "asked for a call of 'a'"),
        ("auto", False, ("a",), None),
        ("auto", False, ("a", "b"), "one tool call at a time"),
    ],
)
def test_the_scripted_model_keeps_to_what_a_request_allows(tool_choice, parallel, calls, said):
    unserved = _unserved(tool_choice, parallel, _calls(*calls))
    assert (unserved is None) if said is None else (said in unserved)
