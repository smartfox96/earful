"""The scripted model written as rules: how a context is read and which rule answers."""

from pathlib import Path

import livekit.agents as lk
import pytest

from earful.core.ledger import first_difference, normalize
from earful.livekit.cases import BOOKING_ACK, BOOKING_INTENT, CASES
from earful.livekit.harness import InvalidRun, run_case
from earful.livekit.providers import Claim, Reply, ToolCall
from earful.scripted.playbook import (
    Context,
    NoRule,
    ScriptedModel,
    ToolResult,
    reply,
    tool_failed,
    tool_returned,
    tool_succeeded,
    user_ever_said,
    user_said,
)

GOLDEN = Path(__file__).parent / "golden"


def user(text):
    return {"type": "message", "role": "user", "text": text}


def assistant(text):
    return {"type": "message", "role": "assistant", "text": text}


def result(name, output="ok", *, failed=False, call_id="c1"):
    return {
        "type": "function_call_output",
        "name": name,
        "output": output,
        "call_id": call_id,
        "is_error": failed,
    }


CALL = {"type": "function_call", "name": "book", "arguments": "{}", "call_id": "c1"}


def test_a_context_is_the_items_read_once():
    context = Context.of(
        [
            {"type": "message", "role": "system", "text": "You take reservations."},
            user("Book a table."),
            assistant("One moment."),
            CALL,
            result("book", "R1"),
            user("And a second one."),
            result("book", "full", failed=True, call_id="c2"),
        ]
    )
    assert context.user_turns == ("Book a table.", "And a second one.")
    assert context.last_user_turn == "And a second one."
    assert context.assistant_turns == ("One moment.",)
    assert context.tool_results == (
        ToolResult("book", "R1", "c1", failed=False),
        ToolResult("book", "full", "c2", failed=True),
    )
    assert context.results_of("refund") == () and len(context.results_of()) == 2


def test_an_empty_context_has_no_turns_and_an_empty_last_turn():
    context = Context.of([])
    assert context.user_turns == () and context.last_user_turn == ""
    assert context.tool_results == ()


def test_a_message_without_text_reads_as_empty_text():
    assert Context.of([{"type": "message", "role": "user", "text": None}]).user_turns == ("",)


def test_user_said_reads_the_latest_turn_and_user_ever_said_reads_every_turn():
    context = Context.of([user("Book a TABLE for six."), user("Thanks.")])
    assert not user_said("table")(context)
    assert user_said("thanks")(context) and user_said("table", "THANK")(context)
    assert user_ever_said("table")(context)
    assert not user_ever_said("window")(context)
    with pytest.raises(ValueError, match="at least one phrase"):
        user_said()
    with pytest.raises(ValueError, match="at least one phrase"):
        user_ever_said()


def test_tool_conditions_tell_a_result_a_success_and_a_failure_apart():
    nothing = Context.of([user("Book.")])
    succeeded = Context.of([result("book")])
    failed = Context.of([result("book", failed=True)])
    retried = Context.of([result("book", failed=True), result("book", call_id="c2")])
    for condition, holds in (
        (tool_returned("book"), (False, True, True, True)),
        (tool_succeeded("book"), (False, True, False, True)),
        (tool_failed("book"), (False, False, True, False)),
        (tool_returned(), (False, True, True, True)),
        (tool_returned("refund"), (False, False, False, False)),
    ):
        assert tuple(condition(c) for c in (nothing, succeeded, failed, retried)) == holds


def test_conditions_combine_and_say_what_they_are():
    booked_and_thanked = tool_succeeded("book") & user_said("thanks")
    assert booked_and_thanked(Context.of([result("book"), user("Thanks.")]))
    assert not booked_and_thanked(Context.of([user("Thanks.")]))
    either = tool_failed() | user_said("stop")
    assert either(Context.of([user("Stop.")])) and not either(Context.of([user("Go on.")]))
    assert (~tool_returned())(Context.of([])) and not (~tool_returned())(Context.of([result("x")]))
    # A plain function of a context is a condition too, and combines with one.
    twice = tool_returned() & (lambda context: len(context.tool_results) == 2)
    assert twice(Context.of([result("a"), result("b")])) and not twice(Context.of([result("a")]))
    assert booked_and_thanked.name == "(tool_succeeded(book) and user_said('thanks',))"
    assert (~tool_returned()).name == "not tool_returned()"


def test_a_reply_is_built_from_plain_values():
    assert reply("Hello.") == Reply(text="Hello.")
    assert reply(call="book") == Reply(tool_calls=(ToolCall("book", "{}"),))
    assert reply(call=("book", {"party_size": 6})) == Reply(
        tool_calls=(ToolCall("book", '{"party_size": 6}'),)
    )
    keeps_the_id = ToolCall("book", "{}", call_id="mine")
    assert reply(call=keeps_the_id).tool_calls == (keeps_the_id,)
    assert reply(call=[("book", {"n": 1}), "confirm", keeps_the_id]).tool_calls == (
        ToolCall("book", '{"n": 1}'),
        ToolCall("confirm", "{}"),
        keeps_the_id,
    )
    assert reply("Booked.", claims=["book-1", Claim("book-2", 0, 0)]).claims == (
        Claim("book-1"),
        Claim("book-2", 0, 0),
    )
    timed = reply("Slow.", ttft=0.4, duration=1.2)
    assert (timed.ttft, timed.duration) == (0.4, 1.2)
    assert (reply("Fast.").ttft, reply("Fast.").duration) == (Reply().ttft, Reply().duration)


def test_the_first_rule_that_holds_answers_and_otherwise_answers_when_none_does():
    model = (
        ScriptedModel()
        .when(tool_succeeded("book"), say="Your table is booked.", claims=["book-1"])
        .when(tool_failed("book"), say="Sorry, I could not book that.")
        .when(user_ever_said("table"), say="One moment.", call=("book", {"party_size": 6}))
        .otherwise(say="How can I help?")
    )
    assert model([user("Hello.")]) == Reply(text="How can I help?")
    asked = [user("A table please.")]
    assert model(asked).tool_calls == (ToolCall("book", '{"party_size": 6}'),)
    assert model([*asked, result("book", failed=True)]).text == "Sorry, I could not book that."
    booked = model([*asked, result("book", failed=True), result("book", call_id="c2")])
    assert booked == Reply(text="Your table is booked.", claims=(Claim("book-1"),))


def test_a_rule_can_be_given_a_reply_already_built():
    answer = Reply(text="Stopping.", ttft=0.5)
    assert ScriptedModel().when(user_said("stop"), answer)([user("Stop.")]) is answer


def test_a_context_no_rule_matches_is_an_error_that_says_what_was_tried():
    model = ScriptedModel().when(user_said("menu"), say="Soup.")
    with pytest.raises(NoRule, match=r"latest user message is 'Hello.'.*user_said\('menu',\)"):
        model([user("Hello.")])


def test_a_model_is_not_changed_by_adding_to_it():
    base = ScriptedModel().when(user_said("menu"), say="Soup.")
    longer = base.when(user_said("stop"), say="Stopping.").otherwise(say="Pardon?")
    assert len(base.rules) == 1 and base.default is None
    assert len(longer.rules) == 2 and longer.default == Reply(text="Pardon?")
    with pytest.raises(NoRule):
        base([user("Stop.")])
    with pytest.raises(AttributeError):
        base.rules = ()


def test_a_rule_that_could_not_be_reached_or_has_no_reply_is_refused():
    done = ScriptedModel().otherwise(say="Pardon?")
    with pytest.raises(ValueError, match="could never be reached"):
        done.when(user_said("menu"), say="Soup.")
    with pytest.raises(ValueError, match="already given"):
        done.otherwise(say="What?")
    with pytest.raises(ValueError, match="needs a reply"):
        ScriptedModel().when(user_said("menu"))
    with pytest.raises(ValueError, match="not both"):
        ScriptedModel().when(user_said("menu"), Reply(text="Soup."), say="Soup.")


BOOKING_AS_RULES = (
    ScriptedModel()
    .when(tool_returned(), say="Your table is booked.", claims=[BOOKING_INTENT])
    .when(user_ever_said("table"), say=BOOKING_ACK, call=("create_reservation", {"party_size": 6}))
    .otherwise(say="How can I help?")
)


def test_a_scenario_whose_model_is_written_as_rules_gives_the_very_same_ledger():
    case = CASES["book_then_interrupt"]()
    case.policy = BOOKING_AS_RULES
    path = GOLDEN / f"lk-{lk.__version__}.book_then_interrupt.ledger.normalized.jsonl"
    lines = normalize(run_case(case).ledger.events)
    assert first_difference(path.read_text("ascii").splitlines(), lines) is None


def test_a_run_whose_model_has_no_rule_for_a_context_is_invalid():
    case = CASES["barge_in_mid_word"]()
    case.policy = ScriptedModel().when(user_said("menu"), say="We have soup.")
    with pytest.raises(InvalidRun, match="policy raised on call 2: NoRule"):
        run_case(case)  # the second turn is "Stop."
