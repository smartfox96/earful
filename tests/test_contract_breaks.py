"""Each contract detects its deliberate break, and passes its clean control.

Every break is reached through a seam any application has: the model policy, a tool, the
backend, a public agent hook, an agent option or the audio output. Nothing inside the
framework is patched, and the checker sees only the ledger.
"""

import asyncio
import copy

import livekit.agents as lk
import pytest

from earful.core.checker import FAIL, NOT_EXERCISED, PASS, UNOBSERVABLE, check
from earful.core.checker.view import View
from earful.livekit.cases import CASES, _last_user_text
from earful.livekit.harness import Case, Delay, Intent, run_case
from earful.livekit.providers import Claim, Reply, ToolCall, UserSpeech
from earful.livekit.sink import ClockedSink

VERSION = tuple(int(part) for part in lk.__version__.split(".")[:3])
FIX_3702 = (1, 6, 6)  # before it, an interrupted reply loses its tool results

INTENT = "book-table"
BOOK = '{"party_size": 6}'
ASK = UserSpeech(start=0.5, end=2.5, transcript="Book a table for six tonight.")


def _report(case):
    return check(run_case(case).ledger.events)


def _rules(report, contract):
    return sorted({f.rule for v in report.results[contract].failures for f in v.findings})


def _outputs(items):
    return sum(1 for i in items if i["type"] == "function_call_output")


class _Booker(lk.Agent):
    """Books through the backend; how it writes is the seam under test."""

    def __init__(self, backend, *, writes=1, key=None, latency=0.0, hang=False):
        super().__init__(instructions="You take restaurant reservations.")
        self._backend, self._writes, self._key = backend, writes, key
        self._latency, self._hang = latency, hang

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> str:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        if self._hang:
            await asyncio.get_running_loop().create_future()  # never returns, no timeout
        receipt = ""
        for _ in range(self._writes):
            receipt = await self._backend.write(
                INTENT, {"party_size": party_size}, key=self._key, latency=self._latency
            )
        return f"Reservation {receipt} confirmed."


def _booking(policy, *, agent=_Booker, speeches=(ASK,), **kwargs):
    return Case(
        name="break",
        agent=agent,
        speeches=list(speeches),
        policy=policy,
        intents=[Intent(INTENT, tools=("create_reservation",))],
        **kwargs,
    )


def _book_then_confirm(items):
    if _outputs(items):
        return Reply(text="Your table is booked.", claims=(Claim(INTENT),))
    return Reply(
        text="One moment please.", tool_calls=(ToolCall("create_reservation", BOOK, "c1"),)
    )


# --------------------------------------------------------------------------- A1


def _correction(honours: bool):
    def policy(items):
        if "cancel" in _last_user_text(items).lower():
            if not honours and _outputs(items) < 2:  # books again although told to cancel
                return Reply(
                    text="Booking it again.",
                    tool_calls=(ToolCall("create_reservation", BOOK, "c2"),),
                )
            return Reply(text="Okay, cancelled.")
        return _book_then_confirm(items)

    return policy


CANCEL = UserSpeech(start=7.0, end=8.0, transcript="Actually cancel that.", retires=(INTENT,))


def test_a1_fails_when_the_tool_is_called_again_after_the_correction():
    report = _report(_booking(_correction(honours=False), speeches=(ASK, CANCEL)))
    assert report.outcome("A1") == FAIL
    (failed,) = report.results["A1"].failures
    assert failed.instance == {"speech": 1, "intent": INTENT}
    assert failed.measures["writes_after_retirement"] == 1


def test_a1_passes_when_the_correction_is_honoured():
    report = _report(_booking(_correction(honours=True), speeches=(ASK, CANCEL)))
    assert report.outcome("A1") == PASS


# --------------------------------------------------------------------------- A2


def test_a2_fails_when_a_tool_writes_twice_on_a_plain_backend():
    report = _report(_booking(_book_then_confirm, agent=lambda b: _Booker(b, writes=2, key="k1")))
    assert report.outcome("A2") == FAIL
    assert report.results["A2"].failures[0].measures["committed"] == 2


def test_a2_passes_for_the_same_tool_on_a_keyed_backend():
    report = _report(
        _booking(
            _book_then_confirm, agent=lambda b: _Booker(b, writes=2, key="k1"), backend_mode="keyed"
        )
    )
    assert report.outcome("A2") == PASS
    measures = report.results["A2"].verdicts[0].measures
    assert (measures["committed"], measures["deduplicated"]) == (1, 1)


# --------------------------------------------------------------------------- A3


def _claims_while_calling(items):
    if _outputs(items):
        return Reply(text="Anything else?")
    return Reply(
        text="Your table is booked.",
        tool_calls=(ToolCall("create_reservation", BOOK, "c1"),),
        claims=(Claim(INTENT),),
    )


def test_a3_fails_when_success_is_spoken_before_the_backend_commits():
    """The backend takes 5 s to commit; the sentence takes 1.2 s to play."""
    report = _report(
        _booking(_claims_while_calling, agent=lambda b: _Booker(b, latency=5.0), drain_delay=10.0)
    )
    assert report.outcome("A3") == FAIL
    measures = report.results["A3"].failures[0].measures
    assert measures["first_commit_us"] > measures["claim_heard_us"]


def test_a3_passes_when_the_claim_waits_for_the_tool_result():
    report = _report(
        _booking(_book_then_confirm, agent=lambda b: _Booker(b, latency=5.0), drain_delay=10.0)
    )
    assert report.outcome("A3") == PASS
    assert report.results["A3"].verdicts[0].measures["commit_before_claim_us"] > 0


# --------------------------------------------------------------------------- M1


class _LateSink(ClockedSink):
    """Tells the framework playback has started 2.5 s before it begins to play, as an
    output with a buffer the framework does not know about would; records the truth."""

    announced = 0

    def on_playback_started(self, *args, **kwargs):
        type(self).announced += 1
        return super().on_playback_started(*args, **kwargs)

    def _begin(self, start):
        asyncio.get_running_loop().call_later(2.5, start)


def test_m1_fails_when_the_output_plays_later_than_it_says():
    case = CASES["barge_in_mid_word"]()
    case.sink = _LateSink
    report = _report(case)
    assert report.outcome("M1") == FAIL
    assert _rules(report, "M1") in (["rule 3"], ["rule 4"])  # ahead of, or with nothing, played
    row = report.results["M1"].failures[0].measures["contexts"][0]
    assert row["remembered_minus_played_and_cut"] > 1


def test_a_late_output_announces_each_segment_once():
    """A user who speaks before the late output has begun pauses it; resuming must not
    tell the framework a second time that playback started."""
    _LateSink.announced = 0
    case = CASES["speak_over_new_turn"]()
    case.sink = _LateSink
    events = run_case(case).ledger.events
    segments = {e["data"]["segment"] for e in events if e["kind"] == "sink.flush"}
    assert 0 < _LateSink.announced <= len(segments)


def test_m1_passes_with_the_unmodified_output():
    assert _report(CASES["barge_in_mid_word"]()).outcome("M1") == PASS


# --------------------------------------------------------------------------- M2


def _book_once():
    seen = {"calls": 0}

    def policy(items):
        seen["calls"] += 1
        if seen["calls"] == 1:
            return Reply(tool_calls=(ToolCall("create_reservation", BOOK, "c1"),))
        return Reply(text="Your table is booked.")

    return policy


class _ForgetsToolResults(_Booker):
    """A public agent hook that drops tool calls and results before the model sees them."""

    async def llm_node(self, chat_ctx, tools, model_settings):
        kept = [
            i for i in chat_ctx.items if i.type not in ("function_call", "function_call_output")
        ]
        ctx = lk.llm.ChatContext(items=kept)
        async for chunk in lk.Agent.default.llm_node(self, ctx, tools, model_settings):
            yield chunk


class _RepeatsUserTurn(_Booker):
    """A public agent hook that shows the model the last user turn twice."""

    async def llm_node(self, chat_ctx, tools, model_settings):
        items = list(chat_ctx.items)
        last = next(i for i in reversed(items) if i.type == "message" and i.role == "user")
        ctx = lk.llm.ChatContext(items=[*items, last.model_copy(update={"id": "repeated"})])
        async for chunk in lk.Agent.default.llm_node(self, ctx, tools, model_settings):
            yield chunk


def test_m2_fails_when_tool_results_are_removed_from_the_context():
    report = _report(_booking(_book_once(), agent=_ForgetsToolResults))
    assert report.outcome("M2") == FAIL
    assert _rules(report, "M2") == ["rule d"]
    assert report.results["M2"].failures[0].instance == {"context": "call 2"}


def test_m2_fails_when_a_user_turn_is_repeated():
    report = _report(_booking(_book_once(), agent=_RepeatsUserTurn))
    assert report.outcome("M2") == FAIL
    assert "rule a" in _rules(report, "M2")


def test_m2_passes_with_the_unmodified_agent():
    assert _report(_booking(_book_once())).outcome("M2") == PASS


# --------------------------------------------------------------------------- L1


def test_l1_fails_when_a_tool_never_returns():
    report = _report(
        _booking(_book_then_confirm, agent=lambda b: _Booker(b, hang=True), drain_timeout=5.0)
    )
    assert report.outcome("L1") == FAIL
    (failed,) = report.results["L1"].failures
    assert "every tool call finishes: never happened" in failed.findings[0].detail
    assert _rules(report, "L1") == [
        "milestone 2"
    ]  # the finding names the milestone that was missed


def test_l1_passes_when_the_same_tool_returns():
    assert _report(_booking(_book_then_confirm)).outcome("L1") == PASS


def _stuck(**kwargs):
    return _booking(
        _book_then_confirm, agent=lambda b: _Booker(b, hang=True), drain_timeout=5.0, **kwargs
    )


def test_l1_owes_nothing_to_a_user_who_hung_up_before_it_was_due():
    """The same tool that never returns. The user leaves at 4 s, before the tool was due
    to finish, so the turn has no verdict."""
    result = _report(_stuck(hangup=4.0)).results["L1"]
    assert result.outcome == NOT_EXERCISED and not result.verdicts
    assert result.notes == ("speech 0: the user hung up before a response was due",)


def test_l1_still_fails_a_turn_that_was_overdue_when_the_user_left():
    report = _report(_stuck(hangup=20.0))
    assert report.outcome("L1") == FAIL and _rules(report, "L1") == ["milestone 2"]


def test_l1_budgets_a_reply_with_the_delays_declared_for_it():
    """The scenario declares that the first reply's audio takes 3 s to begin and that the
    booking takes 4 s to commit. The turn is slow because it was scripted to be, and its
    budget is built from those delays, not from the defaults."""
    case = _booking(_book_then_confirm, drain_delay=14.0)
    case.delays = [Delay("tts.ttfb", 1, 3.0), Delay("backend.latency", 1, 4.0)]
    events = run_case(case).ledger.events
    assert next(e for e in events if e["kind"] == "playout.started")["t_us"] >= 6_000_000
    report = check(events)
    assert report.outcome("L1") == PASS
    first_audio = next(
        m
        for m in report.results["L1"].verdicts[0].measures["milestones"]
        if m["milestone"].startswith("the first word")
    )
    assert (
        first_audio["deadline_us"] - first_audio["reached_us"] >= 1_000_000
    )  # the slack is still all there


def _silent(items):
    return Reply()


def test_l1_fails_when_the_model_returns_nothing():
    report = _report(_booking(_silent))
    assert report.outcome("L1") == FAIL
    assert _rules(report, "L1") == ["milestone 3"]
    assert (
        "the first word of a reply starts playing: never happened"
        in report.results["L1"].failures[0].findings[0].detail
    )


def test_l1_a_backchannel_does_not_take_the_obligation_away():
    """The user says "uh huh" while a slow tool runs, and the model then says nothing.
    The first turn is still owed a reply, and never gets one."""
    seen = {"calls": 0}

    def policy(items):
        seen["calls"] += 1
        if seen["calls"] == 1:
            return Reply(tool_calls=(ToolCall("create_reservation", BOOK, "c1"),))
        return Reply()

    ack = UserSpeech(start=4.0, end=4.5, transcript="Uh huh.", backchannel=True)
    report = _report(
        _booking(
            policy, agent=lambda b: _Booker(b, latency=3.0), speeches=(ASK, ack), drain_delay=14.0
        )
    )
    assert report.outcome("L1") == FAIL
    (failed,) = report.results["L1"].failures
    assert failed.instance == {"speech": 0}
    assert "the first word of a reply starts playing: never happened" in failed.findings[-1].detail


def test_l1_is_unobservable_when_the_run_ends_before_the_deadline():
    """The same silent model, but the scenario stops 1 s after the turn: too soon to say
    the response is late."""
    report = _report(_booking(_silent, drain_delay=1.0))
    assert report.outcome("L1") == UNOBSERVABLE


def test_l1_does_not_apply_to_a_turn_that_expects_no_reply():
    quiet = UserSpeech(
        start=0.5, end=2.5, transcript="Book a table for six tonight.", expects_reply=False
    )
    assert _report(_booking(_silent, speeches=(quiet,))).outcome("L1") == NOT_EXERCISED


# --------------------------------------------------------------------------- hangup


class _PlaysOn(ClockedSink):
    """Keeps playing for 2.5 s after it is told to pause or clear: audio already in a
    buffer nothing can flush."""

    def _obey(self, action):
        asyncio.get_running_loop().call_later(2.5, action)


def _hung_up_on(sink):
    """A long reply from 3.3 s; the user leaves at 5 s, six words in."""
    case = CASES["barge_in_mid_word"]()
    case.speeches, case.sink, case.hangup = case.speeches[:1], sink, 5.0
    return run_case(case).ledger.events


def test_m1_fails_when_the_output_plays_on_after_the_user_has_left():
    """The output plays on for 2.5 s after the user has left, and the framework remembers
    all of it. The user heard six words; fifteen are remembered as said."""
    events = _hung_up_on(_PlaysOn)
    (finished,) = [e for e in events if e["kind"] == "playout.finished"]
    assert len(finished["data"]["words_played"]) == 15  # what the sink played, to no one
    report = check(events)
    assert report.outcome("M1") == FAIL and _rules(report, "M1") == ["rule 3"]
    (reply,) = report.results["M1"].verdicts
    assert (reply.measures["played"], reply.measures["cut"]) == (6, 1)  # what the user heard
    assert reply.measures["contexts"][0]["remembered"] == 15
    assert "played_on_after_the_hangup" in reply.flags


def test_m1_passes_at_a_hangup_with_the_unmodified_output():
    """The output stops as the user leaves, and the six words played are the six remembered."""
    report = check(_hung_up_on(ClockedSink))
    assert report.outcome("M1") == PASS
    (reply,) = report.results["M1"].verdicts
    assert (reply.measures["played"], reply.measures["contexts"][0]["remembered"]) == (6, 6)
    assert "played_on_after_the_hangup" not in reply.flags


def test_audio_that_starts_as_the_user_leaves_was_never_audible():
    """The same run, read as if the user had left at the instant the reply began."""
    events = copy.deepcopy(_hung_up_on(ClockedSink))
    started = next(e for e in events if e["kind"] == "playout.started")
    next(e for e in events if e["kind"] == "user.hangup")["t_us"] = started["t_us"]
    (playout,) = View(events).playouts.values()
    assert playout.first_audible_us is None and playout.played == 0 and playout.outlasted_hangup


def test_a3_a_claim_that_finished_playing_after_the_user_left_was_not_delivered():
    stays = run_case(_booking(_book_then_confirm, sink=_PlaysOn)).ledger.events
    assert check(stays).outcome("A3") == PASS
    origin = next(e["t_us"] for e in stays if e["kind"] == "user.origin")
    claim_starts = [e["t_us"] for e in stays if e["kind"] == "playout.started"][-1] - origin
    # The user leaves 0.3 s into "Your table is booked."; the output plays the rest to no one.
    report = _report(
        _booking(_book_then_confirm, sink=_PlaysOn, hangup=(claim_starts + 300_000) / 1_000_000)
    )
    result = report.results["A3"]
    assert result.outcome == NOT_EXERCISED
    assert any("was not delivered" in note for note in result.notes)
    # The user heard "Your" and the start of "table"; the output played all four words.
    said = next(v for v in report.results["M1"].verdicts if v.measures["words"] == 4)
    assert (said.measures["played"], said.measures["cut"]) == (1, 1)


# --------------------------------------------------------------------------- L2


class _LaggingSink(ClockedSink):
    """Keeps playing for 400 ms after it is told to pause or clear."""

    def _obey(self, action):
        asyncio.get_running_loop().call_later(0.4, action)


def test_l2_fails_when_the_output_is_slow_to_stop():
    case = CASES["barge_in_mid_word"]()
    case.sink = _LaggingSink
    report = _report(case)
    assert report.outcome("L2") == FAIL
    (failed,) = report.results["L2"].failures
    assert [f.rule for f in failed.findings] == ["rule a"]
    assert failed.measures["decision_lag_us"] == 0  # the framework asked on time
    assert failed.measures["stop_latency_us"] == 400_000  # the output did not obey on time
    assert failed.flags == ()  # so Earful's model of the threshold is not in doubt


def _deaf_host(backend):
    return lk.Agent(instructions="You are a restaurant host.", allow_interruptions=False)


def test_l2_fails_when_the_agent_cannot_be_interrupted_but_the_policy_expects_it():
    case = CASES["barge_in_mid_word"]()
    case.agent = _deaf_host
    report = _report(case)
    assert report.outcome("L2") == FAIL
    (failed,) = report.results["L2"].failures
    assert [f.rule for f in failed.findings] == ["rule a"]
    assert failed.measures["recognition_delay_us"] is None  # no command ever reached the output


def test_l2_passes_when_the_policy_says_interruptions_are_not_allowed():
    case = CASES["barge_in_mid_word"]()
    case.agent = _deaf_host
    case.contracts = {"L2": {"interruptions_allowed": False}}
    assert _report(case).outcome("L2") == PASS


def test_l2_passes_with_the_unmodified_output():
    assert _report(CASES["barge_in_mid_word"]()).outcome("L2") == PASS


# --------------------------------------------------------------------------- ordinary agents
#
# Things real agents do that are not defects. Each once produced a false verdict.


class _Greeter(lk.Agent):
    """Speaks a fixed greeting itself, not through the model."""

    def __init__(self, backend):
        super().__init__(instructions="You are a restaurant host.")

    async def on_enter(self):
        self.session.say("Welcome to the restaurant.")


def test_text_the_application_speaks_itself_is_judged_like_a_reply():
    case = CASES["barge_in_mid_word"]()
    case.agent = _Greeter
    case.speeches = [
        UserSpeech(start=2.5, end=4.5, transcript="Tell me about the menu."),
        UserSpeech(start=7.0, end=8.0, transcript="Stop."),
    ]
    events = run_case(case).ledger.events
    report = check(events)
    assert report.failures == ()
    assert report.outcome("M2") == PASS and report.outcome("M1") == PASS
    greeting = report.results["M1"].verdicts[0]
    assert greeting.instance == {"utterance": 1}
    assert greeting.measures["played"] == greeting.measures["words"] == 4
    assert greeting.measures["contexts"][0]["remembered"] == 4


class _QuietTool(_Booker):
    """Books, then asks the framework not to reply: by returning nothing, or by raising
    the framework's own signal for it."""

    def __init__(self, backend, *, stop):
        super().__init__(backend)
        self._stop = stop

    @lk.function_tool
    async def create_reservation(self, party_size: int) -> None:
        """Create a reservation.

        Args:
            party_size: Number of guests
        """
        await self._backend.write(INTENT, {"party_size": party_size})
        if self._stop:
            raise lk.StopResponse()


def _book_silently(items):
    return Reply(tool_calls=(ToolCall("create_reservation", BOOK, "c1"),))


@pytest.mark.parametrize("stop", [False, True], ids=["returns nothing", "raises StopResponse"])
def test_a_tool_that_asks_for_silence_is_not_a_stalled_turn(stop):
    events = run_case(
        _booking(_book_silently, agent=lambda b: _QuietTool(b, stop=stop))
    ).ledger.events
    outcome = next(e for e in events if e["kind"] in ("tool.returned", "tool.raised"))
    assert outcome["data"]["expects_reply"] is False
    assert (
        len([e for e in events if e["kind"] == "llm.request"]) == 1
    )  # the framework did not reply
    report = check(events)
    assert report.failures == ()
    (turn,) = report.results["L1"].verdicts
    assert turn.outcome == PASS and turn.flags == ("silenced_by_a_tool",)
    assert report.outcome("M2") == PASS


def test_l1_fails_when_tool_rounds_run_out_and_nothing_was_said():
    """Two rounds of tools and then silence. With one round allowed, the turn is owed its
    first word after the first."""
    seen = {"calls": 0}

    def policy(items):
        seen["calls"] += 1
        if seen["calls"] <= 2:
            return Reply(tool_calls=(ToolCall("create_reservation", BOOK, f"c{seen['calls']}"),))
        return Reply()

    case = _booking(policy, contracts={"L1": {"max_steps": 1}})
    case.intents = [Intent(INTENT, tools=("create_reservation",), authorized=2)]
    report = _report(case)
    assert report.outcome("L1") == FAIL
    assert (
        "the first word of a reply starts playing: never happened"
        in report.results["L1"].failures[0].findings[-1].detail
    )


WINDOW = "Is the table by the window?"


def _host_who_books(ack):
    """Books when asked, confirms once the result is the last thing in its context, and
    answers a question about the table."""

    def policy(items):
        if items[-1]["type"] == "function_call_output":
            if items[-1]["is_error"]:
                return Reply(text="I am sorry something went wrong.")
            return Reply(text="Your table is booked.", claims=(Claim(INTENT),))
        if "window" in _last_user_text(items).lower():
            return Reply(text="Yes it is right by the window.")
        if not _outputs(items):
            return Reply(text=ack, tool_calls=(ToolCall("create_reservation", BOOK, "c1"),))
        return Reply(text="How can I help?")

    return policy


def _asks_about_the_window(at):
    return UserSpeech(start=at, end=at + 1.0, transcript=WINDOW)


def _details(report):
    return [f.detail for v in report.failures for f in v.findings]


def test_a_slow_tool_interrupted_while_it_runs_is_not_a_failure():
    """The booking takes 3 s and the user asks something else meanwhile. What the model
    is shown for the call in progress is the framework's choice: livekit-agents 1.8 shows
    a stand-in result, earlier releases show nothing."""
    report = _report(
        _booking(
            _host_who_books("One moment while I book that table for you."),
            agent=lambda b: _Booker(b, latency=3.0),
            speeches=(ASK, _asks_about_the_window(3.4)),
            drain_delay=14.0,
        )
    )
    assert not any("no witnessed tool" in detail for detail in _details(report))
    if VERSION >= FIX_3702:
        assert report.failures == ()


def test_a_reply_made_ahead_of_time_does_not_make_a_later_call_lose_the_turn():
    """The framework answers the second turn as its transcript arrives, before the turn
    is over. The booking then returns, and the call that follows it up is made for the
    first turn: it need not show a transcript the framework is still holding."""
    report = _report(
        _booking(
            _host_who_books(""),
            agent=lambda b: _Booker(b, latency=2.0),
            speeches=(ASK, _asks_about_the_window(3.6)),
            drain_delay=12.0,
        )
    )
    assert "rule b" not in _rules(report, "M2")


def test_a_tool_that_returns_as_the_next_turn_is_answered_is_not_a_lost_result():
    """The booking returns at 5.0 s, the instant the second transcript arrives and the
    framework starts answering it. That answer was put together without the result."""
    report = _report(
        _booking(
            _host_who_books(""),
            agent=lambda b: _Booker(b, latency=2.0),
            speeches=(ASK, _asks_about_the_window(3.8)),
            drain_delay=12.0,
        )
    )
    assert all(v.instance != {"context": "call 2"} for v in report.results["M2"].failures)
    if VERSION >= FIX_3702:
        assert report.failures == ()


def test_l1_a_reply_generated_again_after_a_backchannel_answers_the_turn():
    """The user says "uh huh" before the reply has begun. The framework drops that reply
    and calls the model again; the reply it then speaks is the turn's response."""
    case = CASES["barge_in_mid_word"]()
    case.speeches = [
        case.speeches[0],
        UserSpeech(start=3.1, end=3.7, transcript="Uh huh.", backchannel=True),
    ]
    case.drain_delay = 14.0
    report = _report(case)
    assert report.failures == ()
    (turn,) = report.results["L1"].verdicts
    assert turn.instance == {"speech": 0} and turn.outcome == PASS


def test_a_call_the_framework_refuses_is_progress_and_not_a_lost_result():
    """The model passes an argument the tool cannot take. The framework does not run the
    tool; it tells the model, which apologizes."""

    def policy(items):
        if _outputs(items):
            return Reply(text="I am sorry something went wrong.")
        return Reply(
            text="One moment please.",
            tool_calls=(ToolCall("create_reservation", '{"party_size": "six"}', "c1"),),
        )

    events = run_case(_booking(policy)).ledger.events
    assert not [e for e in events if e["kind"] == "tool.called"]
    report = check(events)
    assert report.failures == ()
    assert report.outcome("L1") == PASS and report.outcome("M2") == PASS
    assert report.results["M2"].verdicts[1].measures["refused_calls"] == 1


def test_l1_a_tool_call_dropped_because_the_user_went_on_is_not_unobservable():
    """With a 2 s endpointing delay the user speaks again before the first turn is over.
    The framework discards what it had generated, tool call included. The tool is
    witnessed, so the ledger shows the call was never made, and the later speech carries
    the obligation."""
    report = _report(
        _booking(
            _host_who_books(""),
            speeches=(ASK, UserSpeech(start=4.0, end=4.8, transcript=WINDOW)),
            min_endpointing_delay=2.0,
            drain_delay=12.0,
        )
    )
    assert report.failures == ()
    assert [v.outcome for v in report.results["L1"].verdicts] == [PASS]
    assert "speech 0" in report.results["L1"].notes[0]


def test_a_reply_the_framework_generates_twice_is_not_unobservable():
    """The framework answers the second turn ahead of time, then again once the booking
    has returned. The scripted model gives the same words both times and one is voiced.
    Which one cannot be observed, and makes no difference to what was heard."""
    report = _report(
        _booking(
            _host_who_books(""),
            agent=lambda b: _Booker(b, latency=2.0),
            speeches=(ASK, _asks_about_the_window(3.6)),
            drain_delay=12.0,
        )
    )
    assert report.readings > 1 and report.reading
    for contract in ("M1", "L1", "A3"):
        assert all(v.outcome != UNOBSERVABLE for v in report.results[contract].verdicts)
    if VERSION >= FIX_3702:
        assert report.failures == ()
