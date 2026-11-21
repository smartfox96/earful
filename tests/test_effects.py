"""The three ways a durable side effect reaches the backend in a run."""

import asyncio

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk
from breaks import BOOKING, TalksToItsServiceItself, as_written_reads_the_error

from earful.core import backend as backends
from earful.core.backend import Effect, effect
from earful.core.checker import PASS, check
from earful.core.ledger import Ledger, run_id
from earful.core.schema import validate
from earful.livekit import tools
from earful.livekit.cases import BOOKING_INTENT
from earful.livekit.harness import Case, Delay, Fault, Intent, run_case
from earful.livekit.providers import Claim, Reply, ToolCall, UserSpeech
from earful.livekit.tools import effect_tool, witnessed
from earful.scripted.clock import now_us, run_virtual
from earful.scripted.playbook import (
    ScriptedModel,
    tool_returned,
    tool_succeeded,
    user_ever_said,
    user_said,
)


def _of(events, kind):
    return [e["data"] for e in events if e["kind"] == kind]


def _kinds(events, *prefixes):
    return [e["kind"] for e in events if e["kind"].startswith(prefixes)]


def _scenario(events):
    return events[0]["data"]["scenario"]


def test_an_agent_with_nothing_of_earful_in_it_runs_with_its_effect_declared():
    events = run_case(as_written_reads_the_error()).ledger.events
    assert validate(events) == []
    assert _kinds(events, "tool.", "effect.") == [
        "tool.called",
        "effect.requested",
        "effect.committed",
        "tool.returned",
    ]
    (requested,) = _of(events, "effect.requested")
    assert requested["intent"] == BOOKING_INTENT and requested["payload"] == {"party_size": 6}
    assert requested["key"] is None and requested["tool"] == 1
    (returned,) = _of(events, "tool.returned")
    assert returned["output"] == "Reservation R1 confirmed for 6 guests."
    report = check(events)
    assert report.failures == ()
    assert report.outcome("A2") == PASS and report.outcome("A3") == PASS


def test_the_header_names_the_tools_whose_body_did_not_run_and_their_intent():
    scenario = _scenario(run_case(as_written_reads_the_error()).ledger.events)
    assert scenario["effects"] == [{"tool": "create_reservation", "intent": BOOKING_INTENT}]
    # The scenario listed no intent: the effect declares it, carried out by its tool.
    assert scenario["intents"] == [
        {"intent": BOOKING_INTENT, "tools": ["create_reservation"], "authorized": 1}
    ]


def test_a_scenario_without_effects_does_not_state_any():
    case = as_written_reads_the_error()
    case.agent, case.effects = lambda: lk.Agent(instructions="t"), {}
    # The model calls a tool this agent has not got, which the header does not depend on.
    assert "effects" not in _scenario(run_case(case, require_valid=False).ledger.events)


def test_an_intent_the_scenario_lists_keeps_what_it_declares_and_gains_the_tool():
    case = as_written_reads_the_error()
    case.intents = [Intent(BOOKING_INTENT, tools=("amend_reservation",), authorized=2)]
    assert _scenario(run_case(case).ledger.events)["intents"] == [
        {
            "intent": BOOKING_INTENT,
            "tools": ["amend_reservation", "create_reservation"],
            "authorized": 2,
        }
    ]


def test_the_payload_the_key_and_the_result_are_what_the_effect_maps_them_to():
    case = as_written_reads_the_error()
    case.effects = {
        "create_reservation": Effect(
            BOOKING_INTENT,
            payload=lambda arguments: {"guests": arguments["party_size"], "source": "voice"},
            key=lambda arguments: f"table-for-{arguments['party_size']}",
            result=lambda receipt, arguments: {"receipt": receipt, **arguments},
        )
    }
    events = run_case(case).ledger.events
    (requested,) = _of(events, "effect.requested")
    assert requested["payload"] == {"guests": 6, "source": "voice"}
    assert requested["key"] == "table-for-6"
    assert _of(events, "tool.returned")[0]["output"] == {"receipt": "R1", "party_size": 6}


def test_by_default_the_tool_returns_the_receipt_and_one_key_serves_every_call():
    case = as_written_reads_the_error()
    case.effects = {"create_reservation": Effect(BOOKING_INTENT, key="one-table")}
    events = run_case(case).ledger.events
    assert _of(events, "effect.requested")[0]["key"] == "one-table"
    assert _of(events, "tool.returned")[0]["output"] == "R1"


def test_an_effect_for_a_tool_the_agent_does_not_have_is_refused_before_anything_runs():
    """A misspelt tool name would leave the real body to run."""
    case = as_written_reads_the_error()
    case.effects = {"create_reservaton": BOOKING}
    with pytest.raises(ValueError, match=r"\['create_reservaton'\].*has \['create_reservation'\]"):
        run_case(case)
    assert run_case(as_written_reads_the_error()).invalid == []  # the next run is unaffected


def test_a_declared_latency_is_the_writes_and_a_scenario_delay_takes_its_place():
    case = as_written_reads_the_error()
    case.effects = {"create_reservation": Effect(BOOKING_INTENT, latency=1.5)}
    assert _of(run_case(case).ledger.events, "effect.requested")[0]["latency_us"] == 1_500_000
    case = as_written_reads_the_error()
    case.effects = {"create_reservation": Effect(BOOKING_INTENT, latency=1.5)}
    case.delays = [Delay("backend.latency", 1, 0.25)]
    assert _of(run_case(case).ledger.events, "effect.requested")[0]["latency_us"] == 250_000


@pytest.mark.parametrize(
    ("fault", "kinds", "error"),
    [
        (
            Fault("backend.error", 1),
            ["tool.called", "effect.requested", "effect.failed", "tool.raised"],
            "BackendError",
        ),
        (
            Fault("backend.drop", 1, 0.5),
            ["tool.called", "effect.requested", "effect.timed_out", "tool.raised"],
            "BackendTimeout",
        ),
        (
            Fault("backend.timeout", 1, 0.5),
            [
                "tool.called",
                "effect.requested",
                "effect.timed_out",
                "tool.raised",
                "effect.committed",
            ],
            "BackendTimeout",
        ),
    ],
)
def test_a_fault_on_the_write_is_what_the_tool_raises(fault, kinds, error):
    """A write whose caller gave up still commits, after the tool has raised."""
    case = as_written_reads_the_error()
    case.faults, case.delays = [fault], [Delay("backend.latency", 1, 2.0)]
    events = run_case(case).ledger.events
    assert validate(events) == []
    assert _kinds(events, "tool.", "effect.") == kinds
    assert _of(events, "tool.raised")[0]["error"] == error
    assert check(events).failures == ()  # this model confirms only a write that succeeded


def test_a_tool_cancelled_while_its_write_is_under_way_is_cancelled_and_the_write_commits():
    @witnessed
    async def create_reservation(party_size: int) -> str:
        raise AssertionError("a test reached the reservation service")

    async def main():
        ledger = Ledger(now_us, run="test")
        backend = backends.EffectBackend(ledger)
        declared = {"create_reservation": Effect(BOOKING_INTENT, latency=2.0)}
        witness_token = tools.install(tools.ToolWitness(ledger, declared))
        backend_token = backends.install(backend)
        try:
            caller = asyncio.ensure_future(create_reservation(6))
            await asyncio.sleep(0.5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await backend.settle_in_flight()
        finally:
            backends.uninstall(backend_token)
            tools.uninstall(witness_token)
        return [(e["kind"], e["t_us"]) for e in ledger.events]

    assert run_virtual(main) == [
        ("tool.called", 0),
        ("effect.requested", 0),
        ("tool.cancelled", 500_000),
        ("effect.committed", 2_000_000),
    ]


def test_the_user_hangs_up_during_the_write_and_the_commit_is_recorded_apart_from_the_return():
    """The framework lets the tool finish after the user has left."""
    case = as_written_reads_the_error()
    case.delays, case.hangup = [Delay("backend.latency", 1, 2.0)], 3.5
    events = run_case(case).ledger.events
    assert validate(events) == []
    wanted = ("tool.called", "effect.requested", "user.hangup", "effect.committed", "tool.returned")
    happened = [e for e in events if e["kind"] in wanted]
    assert [e["kind"] for e in happened] == list(wanted)
    assert [e["source"] for e in happened] == ["world", "backend", "world", "backend", "world"]
    assert happened[3]["t_us"] - happened[1]["t_us"] == 2_000_000


class _DeclaresItsEffect(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")
        self.reached_the_service = 0

    @lk.function_tool
    @effect_tool(BOOKING_INTENT, key=lambda arguments: f"table-for-{arguments['party_size']}")
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        self.reached_the_service += 1
        return f"Booked for {party_size} by the service itself."


def _declared_at_the_tool(**changes) -> Case:
    case = as_written_reads_the_error()
    case.agent, case.effects = _DeclaresItsEffect, {}
    for name, to in changes.items():
        setattr(case, name, to)
    return case


def test_a_tool_that_declares_its_own_effect_makes_the_write_in_a_run():
    built = []

    def agent():
        built.append(_DeclaresItsEffect())
        return built[-1]

    events = run_case(_declared_at_the_tool(agent=agent)).ledger.events
    assert validate(events) == []
    assert built[0].reached_the_service == 0
    assert _scenario(events)["effects"] == [
        {"tool": "create_reservation", "intent": BOOKING_INTENT}
    ]
    assert len(_of(events, "tool.called")) == 1  # witnessed once, not again by the harness
    assert _of(events, "effect.requested")[0]["key"] == "table-for-6"
    assert _of(events, "tool.returned")[0]["output"] == "R1"
    assert check(events).failures == ()


def test_outside_a_run_the_tool_is_the_tool_as_written():
    agent = _DeclaresItsEffect()
    (tool,) = agent.tools
    assert asyncio.run(tool(party_size=4)) == "Booked for 4 by the service itself."
    assert agent.reached_the_service == 1


def test_the_scenarios_declaration_is_used_where_the_tool_has_one_too():
    other = Effect(
        BOOKING_INTENT,
        key="the-scenarios-key",
        result=lambda receipt, arguments: f"The scenario says {receipt}.",
    )
    events = run_case(_declared_at_the_tool(effects={"create_reservation": other})).ledger.events
    assert validate(events) == []
    assert _scenario(events)["effects"] == [
        {"tool": "create_reservation", "intent": BOOKING_INTENT}
    ]
    assert _of(events, "effect.requested")[0]["key"] == "the-scenarios-key"
    assert _of(events, "tool.returned")[0]["output"] == "The scenario says R1."


def test_a_scenario_that_declares_another_intent_for_the_tool_replaces_the_tools_own():
    def policy(items):
        if any(i["type"] == "function_call_output" for i in items):
            return Reply(text="That is done.")
        return Reply(tool_calls=(ToolCall("create_reservation", '{"party_size": 6}'),))

    case = _declared_at_the_tool(
        policy=policy, effects={"create_reservation": Effect("hold-table-1")}
    )
    events = run_case(case).ledger.events
    assert validate(events) == []
    assert _scenario(events)["effects"] == [
        {"tool": "create_reservation", "intent": "hold-table-1"}
    ]
    assert [it["intent"] for it in _scenario(events)["intents"]] == ["hold-table-1"]
    assert _of(events, "effect.requested")[0]["intent"] == "hold-table-1"


class _RegisteredUnderAnotherName(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You take restaurant reservations.")

    @lk.function_tool(name="create_reservation")
    @effect_tool(BOOKING_INTENT)
    async def book_a_table(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        raise AssertionError("a test reached the reservation service")


def test_a_tool_is_recorded_under_the_name_the_model_calls_it_by():
    events = run_case(_declared_at_the_tool(agent=_RegisteredUnderAnotherName)).ledger.events
    assert validate(events) == []
    assert _scenario(events)["witnessed_tools"] == ["create_reservation"]
    assert {e["data"]["name"] for e in events if e["kind"].startswith("tool.")} == {
        "create_reservation"
    }
    other = Effect(BOOKING_INTENT, key="the-scenarios-key")
    case = _declared_at_the_tool(
        agent=_RegisteredUnderAnotherName, effects={"create_reservation": other}
    )
    assert _of(run_case(case).ledger.events, "effect.requested")[0]["key"] == "the-scenarios-key"


def test_an_effect_without_an_intent_or_with_a_negative_latency_is_refused():
    with pytest.raises(ValueError, match="names its intent"):
        Effect("")
    with pytest.raises(ValueError, match="cannot be negative"):
        Effect(BOOKING_INTENT, latency=-0.1)


def test_effect_tool_takes_the_place_of_witnessed():
    with pytest.raises(TypeError, match="already witnessed"):

        @effect_tool(BOOKING_INTENT)
        @witnessed
        async def create_reservation(party_size: int) -> str:
            return "done"


def test_a_call_the_tool_cannot_take_raises_as_the_tool_would():
    """The write is not made for a call that could not have reached the tool's body."""

    @effect_tool("book")
    async def create_reservation(party_size: int) -> str:
        return "done"

    async def main():
        ledger = Ledger(now_us, run="test")
        tokens = (
            tools.install(tools.ToolWitness(ledger)),
            backends.install(backends.EffectBackend(ledger)),
        )
        try:
            with pytest.raises(TypeError):
                await create_reservation(6, 7)
        finally:
            backends.uninstall(tokens[1])
            tools.uninstall(tokens[0])
        return [e["kind"] for e in ledger.events]

    assert run_virtual(main) == ["tool.called", "tool.raised"]


CANCELS = (
    ScriptedModel()
    .when(user_said("cancel"), say="Okay, cancelled.")
    .when(tool_succeeded(), say="Your table is booked.", claims=[BOOKING_INTENT])
    .when(
        user_ever_said("table"), say="One moment.", call=("create_reservation", {"party_size": 6})
    )
    .otherwise(say="How can I help?")
)
ASK = UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")


def _cancelled(agent, effects, retires=(BOOKING_INTENT,)):
    """A booking, then "cancel that", which retires the booking's intent."""
    cancel = UserSpeech(start=7.0, end=8.0, transcript="Actually cancel that.", retires=retires)
    return Case(
        name="cancelled", agent=agent, effects=effects, speeches=[ASK, cancel], policy=CANCELS
    )


@pytest.mark.parametrize(
    ("agent", "effects"),
    [(TalksToItsServiceItself, {"create_reservation": BOOKING}), (_DeclaresItsEffect, {})],
    ids=["declared by the scenario", "declared at the tool"],
)
def test_a_speech_can_retire_an_intent_that_only_an_effect_declares(agent, effects):
    case = _cancelled(agent, effects)
    assert case.intents == []
    events = run_case(case).ledger.events
    assert validate(events) == []
    assert _scenario(events)["speeches"][1]["retires"] == [BOOKING_INTENT]
    report = check(events)
    assert report.outcome("A1") == PASS and report.failures == ()
    (judged,) = report.results["A1"].verdicts
    assert judged.instance == {"speech": 1, "intent": BOOKING_INTENT}


def test_a_speech_that_retires_an_intent_nothing_declares_is_refused():
    case = _cancelled(TalksToItsServiceItself, {"create_reservation": BOOKING}, ("refund-1",))
    with pytest.raises(
        ValueError, match=r"speech at 7.0 s retires undeclared intents \['refund-1'\]"
    ):
        run_case(case)
    with pytest.raises(ValueError, match="retires undeclared intents"):
        run_case(_cancelled(lambda: lk.Agent(instructions="t"), {}))


class _Orders:
    """Stands in for an ordering service."""

    async def place(self, dish: str) -> str:
        return await effect("order-1", {"dish": dish}, key=f"order-{dish}")


class _Waiter(lk.Agent):
    def __init__(self, orders) -> None:
        super().__init__(instructions="You take orders.")
        self._orders = orders

    @lk.function_tool
    async def order(self, dish: str) -> str:
        """Order a dish.

        Args:
            dish: What to order
        """
        return f"Order {await self._orders.place(dish)} is in."


def test_a_stand_in_the_agent_is_handed_writes_to_the_backend_of_the_run():
    def policy(items):
        if any(i["type"] == "function_call_output" for i in items):
            return Reply(text="It is on its way.")
        return Reply(tool_calls=(ToolCall("order", '{"dish": "soup"}'),))

    case = Case(
        name="stand_in",
        agent=lambda: _Waiter(_Orders()),
        speeches=[UserSpeech(start=0.5, end=1.5, transcript="The soup please.")],
        policy=policy,
        intents=[Intent("order-1", tools=("order",))],
    )
    events = run_case(case).ledger.events
    assert validate(events) == []
    (requested,) = _of(events, "effect.requested")
    assert requested["tool"] == 1 and requested["key"] == "order-soup"
    assert _of(events, "tool.returned")[0]["output"] == "Order R1 is in."  # the tool's own code
    assert "effects" not in _scenario(events)
    assert check(events).outcome("A2") == PASS


def test_two_runs_do_not_share_a_backend():
    receipts = [
        _of(run_case(as_written_reads_the_error()).ledger.events, "effect.committed")[0]["receipt"]
        for _ in range(2)
    ]
    assert receipts == ["R1", "R1"]


def test_an_agent_class_is_a_factory():
    case = as_written_reads_the_error()
    assert case.agent is TalksToItsServiceItself
    assert run_case(case).invalid == []


@pytest.fixture(scope="module")
def recorded():
    return run_case(as_written_reads_the_error()).ledger.events


def _with_header(events, change):
    """``events`` with the scenario header changed, and the run id that goes with it."""
    import copy

    events = copy.deepcopy(events)
    change(events[0]["data"]["scenario"])
    run = run_id(events[0]["data"])
    for event in events:
        event["run"] = run
    return events


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda s: s["effects"].append({"tool": "create_reservation", "intent": BOOKING_INTENT}),
            "more than one effect",
        ),
        (lambda s: s["effects"][0].update(tool="refund"), "'refund', which is not witnessed"),
        (lambda s: s["effects"][0].update(intent="refund-1"), "undeclared intent 'refund-1'"),
        (lambda s: s["intents"][0].update(tools=[]), "not among the tools of its intent"),
    ],
)
def test_a_header_whose_effects_do_not_fit_its_tools_and_intents_is_not_a_ledger(
    recorded, change, message
):
    assert any(message in error for error in validate(_with_header(recorded, change)))


def test_a_ledger_that_states_no_effects_is_valid(recorded):
    assert validate(_with_header(recorded, lambda s: s.pop("effects"))) == []


class _Books(lk.Agent):
    def __init__(self, chat_ctx):
        super().__init__(instructions="You book tables.", chat_ctx=chat_ctx)

    @lk.function_tool
    async def book(self, party_size: int) -> str:
        """Book a table.

        Args:
            party_size: Number of guests
        """
        return await effect("book", {"party_size": party_size})


class _Greets(lk.Agent):
    takes = 0.0  # seconds the handoff takes

    def __init__(self):
        super().__init__(instructions="You greet the caller.")

    @lk.function_tool
    async def transfer(self) -> lk.Agent:
        """Hand the caller to the agent that books."""
        await asyncio.sleep(self.takes)
        return _Books(self.chat_ctx)


class _GreetsSlowly(_Greets):
    takes = 1.0


def _handoff():
    model = (
        ScriptedModel()
        .when(tool_succeeded("book"), say="Your table is booked.", claims=["book"])
        .when(user_said("two"), call=("book", {"party_size": 2}))
        .when(tool_succeeded("transfer"), say="How many guests?")
        .otherwise(call=("transfer", {}))
    )
    return Case(
        name="handoff",
        agent=_Greets,
        speeches=[
            UserSpeech(0.5, 1.5, "I would like a table."),
            UserSpeech(8.0, 9.5, "For two, please."),
        ],
        policy=model,
        intents=[Intent("book", tools=("book",))],
    )


@pytest.mark.parametrize(
    ("agent", "second"),
    [
        (_Greets, (8.0, 9.5)),  # the user speaks well after the handoff at 2.0 s
        (_Greets, (2.5, 4.5)),  # just after it
        (_GreetsSlowly, (2.5, 4.5)),  # across it: the handoff comes at 3.0 s
    ],
)
def test_the_scripted_user_and_the_witness_follow_a_handoff_to_the_next_agent(agent, second):
    case = _handoff()
    case.agent, case.speeches[1] = agent, UserSpeech(*second, "For two, please.")
    result = run_case(case)
    events = result.ledger.events
    assert [d["speech"] for d in _of(events, "user.vad_start")] == [0, 1]
    assert [d["speech"] for d in _of(events, "user.vad_end")] == [0, 1]
    assert [d["text"] for d in _of(events, "user.transcript_final")] == [
        "I would like a table.",
        "For two, please.",
    ]
    called = {d["name"]: d["tool"] for d in _of(events, "tool.called")}
    assert list(called) == ["transfer", "book"]
    (requested,) = _of(events, "effect.requested")
    assert requested["tool"] == called["book"]
    assert result.report.failures == ()
    outcomes = result.report.outcomes()
    assert all(outcomes[contract] == PASS for contract in ("A2", "A3", "M1", "M2", "L1", "L2"))


def _greets_and_returns(returned, then=_Books):
    class Greets(lk.Agent):
        def __init__(self):
            super().__init__(instructions="You greet the caller.")

        @lk.function_tool
        async def transfer(self):
            """Hand the caller to the agent that books."""
            return returned(then(self.chat_ctx))

    return Greets


@pytest.mark.parametrize(
    ("returned", "text", "replies"),
    [
        (lambda agent: agent, "", False),
        (lambda agent: (agent, None), "", False),
        (lambda agent: (agent, "One moment."), "One moment.", True),
        (lambda agent: ["One moment.", agent], "One moment.", True),
        (lambda agent: (agent, "One", "moment."), "['One', 'moment.']", True),
    ],
)
def test_a_tool_that_returns_an_agent_is_shown_to_the_model_by_what_came_with_it(
    returned, text, replies
):
    case = _handoff()
    case.agent = _greets_and_returns(returned)
    result = run_case(case)
    events = result.ledger.events
    handed, booked = _of(events, "tool.returned")
    assert (handed["text"], handed["expects_reply"], handed["handoff"]) == (text, replies, True)
    assert "handoff" not in booked
    shown = [
        item["output"]
        for item in _of(events, "history.final")[0]["items"]
        if item["type"] == "function_call_output" and item["name"] == "transfer"
    ]
    assert shown == [text]
    # A value beside the agent is followed up by the agent that made the call, at once.
    before_the_user_speaks_again = [
        e for e in events if e["kind"] == "llm.request" and e["t_us"] < 8_000_000
    ]
    assert len(before_the_user_speaks_again) == 1 + replies
    assert result.report.failures == ()
    first_turn = result.report.results["L1"].verdicts[0]
    assert first_turn.outcome == PASS
    assert first_turn.flags == (() if replies else ("silenced_by_a_tool",))
    assert result.report.outcome("M2") == PASS


class _BooksAndAsks(_Books):
    async def on_enter(self):
        self.session.generate_reply()


def _asks_on_entering(items):
    results = [i for i in items if i["type"] == "function_call_output"]
    said = [i["text"] for i in items if i["type"] == "message" and i["role"] == "user"]
    if results:
        return Reply(text="Your table is booked.", claims=(Claim("book"),))
    if "two" in said[-1]:
        return Reply(tool_calls=(ToolCall("book", '{"party_size": 2}'),))
    if any(i["type"] == "message" and "You book tables." in (i["text"] or "") for i in items):
        return Reply(text="How many guests?")
    return Reply(tool_calls=(ToolCall("transfer", "{}"),))


def test_a_turn_the_agent_handed_over_to_answers_is_not_silenced():
    case = _handoff()
    case.agent = _greets_and_returns(lambda agent: agent, then=_BooksAndAsks)
    case.policy = _asks_on_entering
    result = run_case(case)
    assert result.report.failures == ()
    first_turn = result.report.results["L1"].verdicts[0]
    assert first_turn.outcome == PASS and first_turn.flags == ()
    assert first_turn.measures["time_to_first_audio_us"] is not None
    assert result.report.outcome("M2") == PASS


@lk.function_tool
async def look_up_hours(day: str) -> str:
    """Say when the restaurant is open.

    Args:
        day: A day of the week
    """
    return f"Open from five on {day}."


def _asks_the_hours(**session):
    model = (
        ScriptedModel()
        .when(tool_succeeded(), say="We open at five.")
        .otherwise(call=("look_up_hours", {"day": "Friday"}))
    )
    return Case(
        name="hours",
        agent=lambda: lk.Agent(instructions="You answer questions."),
        speeches=[UserSpeech(0.5, 1.5, "When do you open on Friday?")],
        policy=model,
        session_kwargs=session,
    )


def test_a_tool_the_session_gives_every_agent_is_witnessed():
    result = run_case(_asks_the_hours(tools=[look_up_hours]))
    result.assert_contracts()
    events = result.ledger.events
    assert _scenario(events)["witnessed_tools"] == ["look_up_hours"]
    (called,) = _of(events, "tool.called")
    (returned,) = _of(events, "tool.returned")
    assert called["name"] == "look_up_hours" and called["arguments"] == {"day": "Friday"}
    assert returned["tool"] == called["tool"] and returned["output"] == "Open from five on Friday."


def test_a_tool_offered_to_the_model_without_the_witness_makes_the_run_invalid(monkeypatch):
    monkeypatch.setattr(tools, "witness_session_tools", lambda session: {})
    result = run_case(_asks_the_hours(tools=[look_up_hours]), require_valid=False)
    assert any("['look_up_hours'] without the witness" in why for why in result.invalid)


_HOURS = "the tool takes ['day'] and needs ['day']"


@pytest.mark.parametrize(
    ("call", "why"),
    [
        (
            ("look_up_hour", {"day": "Friday"}),
            "'look_up_hour', which the agent does not offer; it offers ['look_up_hours']",
        ),
        (
            ("look_up_hours", {"date": "Friday"}),
            f"'look_up_hours' with the arguments ['date']; {_HOURS}",
        ),
        (("look_up_hours", {}), f"'look_up_hours' with the arguments []; {_HOURS}"),
        (
            ("look_up_hours", {"day": "Friday", "week": 2}),
            f"'look_up_hours' with the arguments ['day', 'week']; {_HOURS}",
        ),
        (
            ToolCall("look_up_hours", "Friday"),
            "'look_up_hours' with arguments that are not a JSON object: 'Friday'",
        ),
    ],
)
def test_a_scripted_call_the_offered_tools_cannot_take_makes_the_run_invalid(call, why):
    case = _asks_the_hours(tools=[look_up_hours])
    case.policy = ScriptedModel().when(tool_returned(), say="We open at five.").otherwise(call=call)
    result = run_case(case, require_valid=False)
    assert result.invalid == [f"harness error: call 1: the scripted model called {why}"]


def test_a_tool_of_the_agent_handed_over_to_is_offered_only_after_the_handoff():
    case = _handoff()  # as written, the model calls book once transfer has returned
    case.policy = (
        ScriptedModel()
        .when(tool_returned(), say="Done.")
        .otherwise(call=("book", {"party_size": 2}))
    )
    result = run_case(case, require_valid=False)
    (why,) = result.invalid
    assert why.endswith("called 'book', which the agent does not offer; it offers ['transfer']")
