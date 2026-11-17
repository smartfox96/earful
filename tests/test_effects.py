"""How a durable side effect reaches the backend: from a stand-in the agent is handed,
from an effect the scenario declares for a tool, or from one declared where the tool is
defined. In each the agent's constructor is its own."""

import asyncio

import livekit.agents as lk
import pytest
from breaks import BOOKING, TalksToItsServiceItself, as_written_reads_the_error

from earful.core import backend as backends
from earful.core.backend import Effect, effect
from earful.core.checker import PASS, check
from earful.core.ledger import Ledger, run_id
from earful.core.schema import validate
from earful.livekit import tools
from earful.livekit.cases import BOOKING_INTENT
from earful.livekit.clock import now_us, run_virtual
from earful.livekit.harness import Case, Delay, Fault, Intent, run_case
from earful.livekit.providers import Reply, ToolCall, UserSpeech
from earful.livekit.tools import effect_tool, witnessed


def _of(events, kind):
    return [e["data"] for e in events if e["kind"] == kind]


def _kinds(events, *prefixes):
    return [e["kind"] for e in events if e["kind"].startswith(prefixes)]


def _scenario(events):
    return events[0]["data"]["scenario"]


# --------------------------------------------------------------------- declared by the scenario


def test_an_agent_with_nothing_of_earful_in_it_runs_with_its_effect_declared():
    """The tool's own body would reach a real service. The declared write is made in its
    place, and the run is judged like any other."""
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
    assert "effects" not in _scenario(run_case(case).ledger.events)


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
    # The refused run left nothing installed for the next one to find.
    assert backends.active() is None
    assert run_case(as_written_reads_the_error()).invalid == []


# --------------------------------------------------------------------- the write and its caller


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
    """What the backend did and what the tool was told are separate events. A write whose
    caller gave up still commits, after the tool has raised."""
    case = as_written_reads_the_error()
    case.faults, case.delays = [fault], [Delay("backend.latency", 1, 2.0)]
    events = run_case(case).ledger.events
    assert validate(events) == []
    assert _kinds(events, "tool.", "effect.") == kinds
    assert _of(events, "tool.raised")[0]["error"] == error
    assert check(events).failures == ()  # this model confirms only a write that succeeded


def test_a_tool_cancelled_while_its_write_is_under_way_is_cancelled_and_the_write_commits():
    """The caller is cut off 0.5 s into a write that takes 2 s. The tool never returns; the
    backend records the commit all the same, at its own time."""

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
    """The user leaves 0.5 s into a write that takes 2 s. The framework lets the tool
    finish: the backend's commit and the tool's return are two events from two witnesses."""
    case = as_written_reads_the_error()
    case.delays, case.hangup = [Delay("backend.latency", 1, 2.0)], 3.5
    events = run_case(case).ledger.events
    assert validate(events) == []
    wanted = ("tool.called", "effect.requested", "user.hangup", "effect.committed", "tool.returned")
    happened = [e for e in events if e["kind"] in wanted]
    assert [e["kind"] for e in happened] == list(wanted)
    assert [e["source"] for e in happened] == ["world", "backend", "world", "backend", "world"]
    assert happened[3]["t_us"] - happened[1]["t_us"] == 2_000_000


# --------------------------------------------------------------------------- declared at the tool


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
    """The header and the write agree on which declaration was used."""

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
    """The function is ``book_a_table``; the tool is ``create_reservation``. The scenario,
    the header and every tool event use the tool's name."""
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


# --------------------------------------------------------------------------- from a stand-in


class _Orders:
    """Stands in for an ordering service. It is test code, and it writes where the real
    service would."""

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


def test_outside_a_run_there_is_no_backend_to_write_to():
    assert backends.active() is None
    with pytest.raises(RuntimeError, match="outside an Earful run"):
        asyncio.run(effect("order-1", {"dish": "soup"}))


def test_two_runs_do_not_share_a_backend():
    """Each run builds its agent and its backend anew: the second run's first receipt is
    R1 again."""
    receipts = [
        _of(run_case(as_written_reads_the_error()).ledger.events, "effect.committed")[0]["receipt"]
        for _ in range(2)
    ]
    assert receipts == ["R1", "R1"]


def test_an_agent_class_is_a_factory():
    case = as_written_reads_the_error()
    assert case.agent is TalksToItsServiceItself
    assert run_case(case).invalid == []


# --------------------------------------------------------------------------- the validator


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
