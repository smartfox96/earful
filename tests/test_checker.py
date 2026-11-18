"""The checker on recorded and hand-edited ledgers."""

import copy
import json
from pathlib import Path

import pytest

from earful.core.checker import FAIL, NOT_EXERCISED, PASS, UNOBSERVABLE, PolicyError, check
from earful.core.checker.memory import align, favourable, user_turns
from earful.core.checker.report import ContractResult, Finding, split, verdict
from earful.core.checker.view import Speech, Spoken, View, readings
from earful.core.ledger import read_jsonl, run_id

GOLDEN = Path(__file__).parent / "golden"
INTENT = "book-table-1"


def load(release, case):
    return read_jsonl(GOLDEN / f"lk-{release}.{case}.ledger.normalized.jsonl")


def seal(events):
    """Renumber and re-identify an edited ledger, so only the edit is under test."""
    run = run_id(events[0]["data"])
    for seq, event in enumerate(events):
        event["seq"], event["run"] = seq, run
    return events


def of(events, kind, n=0):
    return [e for e in events if e["kind"] == kind][n]


def context(events, call):
    return next(e for e in events if e["kind"] == "llm.request" and e["data"]["call"] == call)[
        "data"
    ]["items"]


def declare(events, **contracts):
    events[0]["data"]["contracts"] = contracts
    return seal(events)


def rules(report, contract):
    return sorted({f.rule for v in report.results[contract].failures for f in v.findings})


@pytest.fixture
def fixed():
    """book_then_interrupt on a release with #3702 repaired: every contract holds."""
    return load("1.8.12", "book_then_interrupt")


@pytest.fixture
def affected():
    return load("1.5.6", "book_then_interrupt")


@pytest.fixture
def barge():
    return load("1.8.12", "barge_in_mid_word")


def _release(path):
    return tuple(int(n) for n in path.name.removeprefix("lk-").split(".")[:3])


@pytest.mark.parametrize(
    "path", sorted(GOLDEN.glob("*.jsonl")), ids=lambda p: p.name.rsplit(".", 3)[0]
)
def test_reference_ledgers_get_the_documented_outcomes(path):
    release, case = _release(path), path.name.split(".")[3]
    expected = {
        "A1": NOT_EXERCISED,
        "A2": NOT_EXERCISED,
        "A3": NOT_EXERCISED,
        "M1": PASS,
        "M2": PASS,
        "L1": PASS,
        "L2": PASS,
    }
    if case == "book_then_interrupt":
        has_3702 = release < (1, 6, 6)
        expected.update(A2=FAIL if has_3702 else PASS, A3=PASS, M2=FAIL if has_3702 else PASS)
    if case == "speak_over_new_turn" and release < (1, 5, 7):
        expected["L2"] = FAIL
    assert check(read_jsonl(path)).outcomes() == expected


@pytest.mark.parametrize(
    "path", sorted(GOLDEN.glob("*.jsonl")), ids=lambda p: p.name.rsplit(".", 3)[0]
)
def test_the_checker_never_changes_the_ledger_it_is_given(path):
    events = read_jsonl(path)
    before = copy.deepcopy(events)
    check(events)
    assert events == before


def test_an_invalid_run_yields_no_verdicts(fixed):
    fixed[-1]["data"]["stamp_errors"] = 1
    report = check(fixed)
    assert not report.valid and report.results == {}
    assert "reached the sink altered" in report.invalid[0]


def test_a_verdict_cites_the_events_and_witnesses_it_rests_on(affected):
    report = check(affected)
    (failed,) = report.results["A2"].failures
    kinds = {affected[seq]["kind"] for seq in failed.evidence}
    assert kinds == {"effect.requested", "effect.committed"}
    assert failed.witnesses == ("backend",)
    assert all("framework" not in v.witnesses for r in report.results.values() for v in r.verdicts)
    json.dumps(report.to_json())  # the whole report is plain JSON
    assert report.to_json()["contracts"] == "earful.contracts/1"


@pytest.mark.parametrize(
    "contracts, message",
    [
        ({"M1": {"ahead_uss": 0}}, "unknown policy values"),
        ({"M1": {"ahead_us": "a while"}}, "wrong kind of value"),
        ({"L2": {"stop_bound_us": -1}}, "wrong kind of value"),
        ({"A3": {"claim_point": "middle"}}, "must be one of"),
    ],
)
def test_a_policy_value_the_contract_does_not_define_is_refused(fixed, contracts, message):
    with pytest.raises(PolicyError, match=message):
        check(declare(fixed, **contracts))


def retire(events, speech=1):
    events[0]["data"]["scenario"]["speeches"][speech]["retires"] = [INTENT]
    return seal(events)


def test_a1_a_write_requested_before_the_retirement_is_not_a_violation(fixed):
    assert check(retire(fixed)).outcome("A1") == PASS


def test_a1_a_write_requested_after_the_retirement_fails_unless_reissue_is_allowed(affected):
    report = check(retire(affected))
    assert report.outcome("A1") == FAIL
    assert report.results["A1"].failures[0].measures["writes_after_retirement"] == 1
    allowed = declare(retire(copy.deepcopy(affected)), A1={"reissue_allowed": [INTENT]})
    assert check(allowed).outcome("A1") == PASS


def _commit_after_retirement(events):
    """Move the commit to just after the second speech's transcript: an in-flight write."""
    events = retire(events)
    commit = of(events, "effect.committed")
    events.remove(commit)
    final = of(events, "user.transcript_final", 1)
    commit["t_us"] = final["t_us"]
    events.insert(events.index(final) + 1, commit)
    return seal(events)


def test_a1_an_in_flight_write_may_finish_by_default(fixed):
    report = check(_commit_after_retirement(fixed))
    (v,) = report.results["A1"].verdicts
    assert v.outcome == PASS and v.flags == ("late_commit",)


def test_a1_an_in_flight_write_fails_when_the_policy_forbids_it(fixed):
    events = declare(_commit_after_retirement(fixed), A1={"in_flight": "must_not_commit"})
    report = check(events)
    assert report.outcome("A1") == FAIL and rules(report, "A1") == ["in-flight"]


def _without_backend(events):
    return seal([e for e in events if not e["kind"].startswith("effect.")])


def test_a1_and_a2_are_unobservable_when_the_tool_does_not_write_through_the_backend(affected):
    report = check(_without_backend(retire(affected)))
    assert report.outcome("A1") == UNOBSERVABLE
    assert report.outcome("A2") == UNOBSERVABLE


def test_a2_counts_against_the_authorized_number(affected):
    affected[0]["data"]["scenario"]["intents"][0]["authorized"] = 2
    assert check(seal(affected)).outcome("A2") == PASS


def test_a3_fails_when_nothing_was_committed_before_the_claim_was_heard(fixed):
    fixed.remove(of(fixed, "effect.committed"))
    report = check(seal(fixed))
    assert report.outcome("A3") == FAIL
    assert "never committed" in report.results["A3"].failures[0].findings[0].detail


def test_a3_a_claim_whose_last_word_was_not_played_was_not_made(fixed):
    finished = of(fixed, "playout.finished", 1)["data"]
    finished["word_cut"] = finished["words_played"].pop()
    finished["words_completed_us"].pop()
    result = check(fixed).results["A3"]
    assert result.outcome == NOT_EXERCISED
    assert "was not delivered" in result.notes[0]


def test_a3_can_time_the_claim_at_the_start_of_its_first_word(fixed):
    started = of(fixed, "playout.started", 1)["t_us"]
    report = check(declare(fixed, A3={"claim_point": "span_start"}))
    assert report.results["A3"].verdicts[0].measures["claim_heard_us"] == started


def test_a3_and_m1_are_unobservable_when_the_audio_cannot_be_attributed(fixed):
    """The unmatched text may be the reply still unspoken, altered on its way to the voice."""
    synthesized = of(fixed, "tts.synthesized", 1)["data"]
    synthesized.update(call=None, attribution="none", candidates=[])
    report = check(fixed)
    assert report.outcome("A3") == UNOBSERVABLE
    assert [v.outcome for v in report.results["M1"].verdicts] == [PASS, UNOBSERVABLE, UNOBSERVABLE]


def remembered(events, call):
    return next(
        i for i in context(events, call) if i["type"] == "message" and i["role"] == "assistant"
    )


def test_m1_the_tolerance_is_audio_time_and_a_policy_choice(barge):
    """Nine words played and the tenth was cut; two words further is 0.6 s of audio ahead."""
    said = [
        "Tonight",
        "we",
        "have",
        "a",
        "roasted",
        "squash",
        "soup",
        "to",
        "start",
        "then",
        "grilled",
        "sea",
    ]
    message = remembered(barge, 2)
    for kept, drift in ((9, 0), (10, 0), (12, 600_000)):
        message["text"] = " ".join(said[:kept])
        row = check(copy.deepcopy(barge)).results["M1"].verdicts[0].measures["contexts"][0]
        assert (row["remembered"], row["drift_us"]) == (kept, drift)
    assert check(copy.deepcopy(barge)).outcome("M1") == PASS  # within the default second
    report = check(declare(barge, M1={"ahead_us": 500_000}))
    assert report.outcome("M1") == FAIL and rules(report, "M1") == ["rule 3"]


def test_m1_reports_the_measured_difference_on_every_instance(fixed):
    row = check(fixed).results["M1"].verdicts[0].measures["contexts"][0]
    assert (row["remembered"], row["drift_us"]) == (10, 0)
    assert (row["remembered_minus_played"], row["remembered_minus_played_and_cut"]) == (0, -1)


def test_m1_fails_when_memory_falls_behind_what_was_played(barge):
    item = remembered(barge, 2)
    item["text"] = " ".join(item["text"].split()[:3])  # six played words, 1.5 s, forgotten
    report = check(copy.deepcopy(barge))
    assert rules(report, "M1") == ["rule 2"]
    assert report.results["M1"].failures[0].measures["contexts"][0]["drift_us"] == -1_500_000
    assert check(declare(barge, M1={"behind_us": 1_500_000})).outcome("M1") == PASS


def test_m1_fails_when_a_reply_that_never_played_is_remembered():
    events = load("1.8.12", "speak_over_new_turn")  # the stale reply was cleared before it played
    context(events, 2).append(
        {
            "type": "message",
            "id": "x",
            "role": "assistant",
            "text": "Tonight we have",
            "interrupted": True,
        }
    )
    report = check(events)
    assert rules(report, "M1") == ["rule 4"]


def test_m1_checks_the_interrupted_mark_unless_told_not_to(barge):
    remembered(barge, 2)["interrupted"] = False
    assert rules(check(copy.deepcopy(barge)), "M1") == ["rule 5"]
    assert check(declare(barge, M1={"check_interrupted_flag": False})).outcome("M1") == PASS


def _last_reply(events):
    """The final history's record of the last reply, and the segment that played it."""
    history = of(events, "history.final")["data"]["items"]
    message = [i for i in history if i["type"] == "message" and i["role"] == "assistant"][-1]
    return message, of(events, "playout.finished", 1)["data"]


def test_m1_a_reply_cleared_in_the_silence_after_its_last_word_is_interrupted(barge):
    message, segment = _last_reply(barge)
    message["interrupted"] = True
    assert rules(check(copy.deepcopy(barge)), "M1") == ["rule 5"]
    segment["interrupted"] = True
    assert check(barge).outcome("M1") == PASS


def test_m1_the_final_history_counts_only_if_read_after_the_playout_finished(barge):
    finished = of(barge, "playout.finished", 1)
    history = of(barge, "history.final")
    barge.remove(finished)
    finished["t_us"] = history["t_us"]
    finished["data"]["words_completed_us"] = finished["data"]["words_completed_us"][:]
    barge.insert(barge.index(history) + 1, finished)
    history["data"]["items"] = [
        i for i in history["data"]["items"] if i.get("text") != "Okay, stopping."
    ]
    last = check(seal(barge)).results["M1"].verdicts[-1]
    assert last.outcome == UNOBSERVABLE
    assert "read before its playout finished" in last.findings[0].detail


@pytest.mark.parametrize("off_us, either", [(-1, False), (0, True), (1, False)])
def test_m1_a_stop_that_lands_as_the_audio_ends_accepts_either_mark(barge, off_us, either):
    message, _segment = _last_reply(barge)
    message["interrupted"] = True
    finished = of(barge, "playout.finished", 1)
    clear = copy.deepcopy(of(barge, "sink.clear"))
    clear["data"]["segment"] = None
    barge.append({**clear, "t_us": finished["t_us"] + off_us})
    last = check(seal(sorted(barge, key=lambda e: e["t_us"]))).results["M1"].verdicts[-1]
    assert ("stopped_as_it_ended" in last.flags) == either
    assert [f.rule for f in last.findings] == ([] if either else ["rule 5"])


def _stopped_with_nothing_left(events, *, pause_off_us=None, cleared_after_us=2_000_000):
    """The last reply plays out and its segment stays open; it is cleared later. A pause
    command reaches the sink ``pause_off_us`` from the instant the audio ended."""
    finished, history = of(events, "playout.finished", 1), of(events, "history.final")
    ended = finished["t_us"]
    paused = copy.deepcopy(of(events, "playout.paused"))
    paused["t_us"] = ended
    paused["data"].update(segment=2, played_us=finished["data"]["pushed_us"])
    clear = copy.deepcopy(of(events, "sink.clear"))
    clear["t_us"] = ended + cleared_after_us
    clear["data"]["segment"] = 2
    events.remove(finished)
    finished["t_us"] = ended + cleared_after_us
    finished["data"]["interrupted"] = True
    events[events.index(history) : events.index(history)] = [paused, clear, finished]
    if pause_off_us is not None:
        pause = copy.deepcopy(of(events, "sink.pause"))
        pause["t_us"] = ended + pause_off_us
        pause["data"]["segment"] = 2
        events.append(pause)
    return seal(sorted(events, key=lambda e: e["t_us"]))


def test_m1_a_reply_cleared_after_its_audio_ended_is_interrupted(barge):
    """Every word played, and the clear came two seconds later: no tie excuses the mark."""
    message, _segment = _last_reply(barge)
    events = _stopped_with_nothing_left(barge)
    last = check(copy.deepcopy(events)).results["M1"].verdicts[-1]
    assert last.measures["played"] == 2 and "stopped_as_it_ended" not in last.flags
    assert [f.rule for f in last.findings] == ["rule 5"]
    message["interrupted"] = True
    last = check(events).results["M1"].verdicts[-1]
    assert last.outcome == PASS and "stopped_as_it_ended" not in last.flags


@pytest.mark.parametrize("off_us, either", [(-1, False), (0, True), (1, False)])
def test_m1_a_pause_as_the_audio_ends_accepts_either_mark_of_a_reply_cleared_later(
    barge, off_us, either
):
    for mark in (False, True):
        events = copy.deepcopy(barge)
        _last_reply(events)[0]["interrupted"] = mark
        events = _stopped_with_nothing_left(events, pause_off_us=off_us)
        last = check(events).results["M1"].verdicts[-1]
        assert ("stopped_as_it_ended" in last.flags) == either
        assert [f.rule for f in last.findings] == ([] if either or mark else ["rule 5"])


def test_m1_a_stop_as_the_sink_reports_the_end_accepts_either_mark(barge):
    """A segment can stay open after its last sample; it ends for the framework when the
    sink says so."""
    message, _segment = _last_reply(barge)
    message["interrupted"] = True
    finished = of(barge, "playout.finished", 1)
    finished["t_us"] += 300_000
    clear = copy.deepcopy(of(barge, "sink.clear"))
    clear["data"]["segment"] = None
    barge.append({**clear, "t_us": finished["t_us"]})
    last = check(seal(sorted(barge, key=lambda e: e["t_us"]))).results["M1"].verdicts[-1]
    assert last.outcome == PASS and "stopped_as_it_ended" in last.flags


def test_m1_a_flush_as_the_audio_ends_is_no_stop(barge):
    message, _segment = _last_reply(barge)
    message["interrupted"] = True
    finished = of(barge, "playout.finished", 1)
    flush = copy.deepcopy(of(barge, "sink.flush", 1))
    barge.insert(barge.index(finished) + 1, {**flush, "t_us": finished["t_us"]})
    assert rules(check(seal(barge)), "M1") == ["rule 5"]


def test_m1_a_stop_as_the_last_word_ends_excuses_nothing_if_a_word_was_skipped(barge):
    message, segment = _last_reply(barge)
    segment.update(words_played=[31], words_completed_us=segment["words_completed_us"][1:])
    finished = of(barge, "playout.finished", 1)
    clear = copy.deepcopy(of(barge, "sink.clear"))
    clear["data"]["segment"] = None
    barge.insert(barge.index(finished) + 1, {**clear, "t_us": finished["t_us"]})
    last = check(seal(barge)).results["M1"].verdicts[-1]
    assert message["interrupted"] is False and last.measures["played"] == 1
    assert [f.rule for f in last.findings] == ["rule 5"]
    assert "stopped_as_it_ended" not in last.flags


def _barely_started(events):
    """The first reply stops 3 us in, before one sample played; its first word is remembered."""
    of(events, "playout.paused")["data"]["played_us"] = 3
    of(events, "playout.finished")["data"].update(
        position_us=3,
        words_played=[],
        words_completed_us=[],
        word_cut=None,
        word_cut_samples=0,
        text_played="",
    )
    for items in (context(events, 2), of(events, "history.final")["data"]["items"]):
        next(i for i in items if i["type"] == "message" and i["role"] == "assistant")["text"] = (
            "Tonight"
        )
    return events


def test_m1_a_reply_stopped_before_one_sample_played_is_judged_by_the_tolerance(barge):
    first = check(_barely_started(barge)).results["M1"].verdicts[0]
    assert first.outcome == PASS
    assert (first.measures["played"], first.measures["cut"]) == (0, 0)
    assert first.measures["contexts"][0]["drift_us"] == 420_000  # "Tonight": seven letters


def test_m1_the_same_memory_of_a_reply_that_never_became_audible_fails(barge):
    events = _barely_started(barge)
    of(events, "playout.finished")["data"].update(position_us=0, audible=False)
    events = [
        e
        for e in events
        if not (e["kind"] in ("playout.started", "playout.paused") and e["data"]["segment"] == 1)
    ]
    assert rules(check(seal(events)), "M1") == ["rule 4"]


def _thing(n, text, *, reply=True):
    key = f"reply {n}" if reply else f"utterance {n}"
    return Spoken(
        key,
        {"reply": n} if reply else {"utterance": n},
        text.split(),
        {"seq": n},
        n if reply else None,
    )


def _said(*texts):
    return [{"type": "message", "role": "assistant", "text": t} for t in texts]


def test_alignment_follows_order_and_never_guesses():
    one, two = _thing(1, "one moment please"), _thing(2, "one moment please")
    three = _thing(3, "all done")
    # both kept: order decides which is which, even though the texts are identical
    both = align(_said("one moment", "one moment please"), [one, two])
    assert both.matched == {"reply 1": 0, "reply 2": 1}
    # only one kept: it could be either reply, so neither is decided
    either = align(_said("one moment please"), [one, two])
    assert either.possible and either.ambiguous == {"reply 1", "reply 2"} and not either.matched
    mixed = align(_said("one moment please", "all done"), [one, two, three])
    assert mixed.matched == {"reply 3": 1} and mixed.ambiguous == {"reply 1", "reply 2"}
    assert align(_said("all done"), [one, three]).absent == {"reply 1"}
    assert not align(_said("all done", "one moment"), [one, three]).possible
    assert align(_said("something else"), [one]).unmatched_items == [0]
    greeting = _thing(1, "welcome to the restaurant", reply=False)
    spoke = align(_said("welcome to the restaurant", "all done"), [greeting, three])
    assert spoke.matched == {"utterance 1": 0, "reply 3": 1}


def test_of_several_readings_of_identical_texts_the_kindest_is_taken():
    said = [_thing(1, "one moment please"), _thing(2, "one moment please")]
    items = _said("one moment")
    prefer = {"reply 1": (1, 0), "reply 2": (0, 0)}  # what a message costs each reply
    assert favourable(items, said, lambda s, m: prefer[s.key] if m else (0, 0)) == {
        "reply 1": None,
        "reply 2": 0,
    }
    assert favourable(items, said, lambda s, m: (0, 0)) == {
        "reply 1": 0,
        "reply 2": None,
    }
    forgotten = {"reply 1": (2, 0), "reply 2": (0, 0)}
    assert (
        favourable(items, said, lambda s, m: prefer[s.key] if m else forgotten[s.key])["reply 1"]
        == 0
    )
    assert favourable(_said("something else"), said, lambda s, m: (0, 0)) is None


def _answered(*calls, t_us=0, ttft_us=0):
    return [
        {"kind": "llm.response", "t_us": t_us, "data": {"call": call, "ttft_us": ttft_us}}
        for call in calls
    ]


def _synthesized(utterance, attribution, candidates, call=None, t_us=0):
    return {
        "kind": "tts.synthesized",
        "t_us": t_us,
        "data": {
            "started_us": t_us,
            "utterance": utterance,
            "attribution": attribution,
            "candidates": candidates,
            "call": call,
        },
    }


def test_a_reading_gives_each_ambiguous_text_one_of_its_candidates_and_no_reply_twice():
    said = _answered(1, 2, 4)
    assert readings([*said, _synthesized(1, "unique", [1], 1)]) == [{}]
    assert readings([*said, _synthesized(1, "ambiguous", [2, 4])]) == [{1: 2}, {1: 4}]
    both = [_synthesized(1, "ambiguous", [2, 4]), _synthesized(2, "ambiguous", [2, 4])]
    assert readings([*said, *both]) == [{1: 2, 2: 4}, {1: 4, 2: 2}]
    one_taken = [_synthesized(1, "ambiguous", [2, 4]), _synthesized(2, "unique", [2], 2)]
    assert readings([*said, *one_taken]) == [{1: 4}]


def test_a_text_is_no_reply_whose_first_token_came_after_it_was_synthesized():
    text = _synthesized(1, "ambiguous", [2, 4], t_us=500)
    # Final metadata can be recorded much later; attribution uses synthesis start.
    text["t_us"] = 900
    text["t_us"] = 900
    early, late = _answered(2, ttft_us=500), _answered(4, t_us=100, ttft_us=401)
    assert readings([*early, *_answered(4, t_us=100, ttft_us=400), text]) == [{1: 2}, {1: 4}]
    assert readings([*early, *late, text]) == [{1: 2}]
    assert readings([*_answered(2, ttft_us=501), *late, text]) == [{}]


def _generated_twice(events):
    """A second model call with the text of call 2, and one voiced text that could be either."""
    request = next(e for e in events if e["kind"] == "llm.request" and e["data"]["call"] == 2)
    response = of(events, "llm.response", 1)
    again = [copy.deepcopy(request), copy.deepcopy(response)]
    for e in again:
        e["data"]["call"] = 3
    events[events.index(response) + 1 : events.index(response) + 1] = again
    of(events, "tts.synthesized", 1)["data"].update(
        call=None, attribution="ambiguous", candidates=[2, 3]
    )
    return seal(events)


def test_a_reply_generated_twice_with_the_same_words_is_read_the_kindest_way(barge):
    report = check(_generated_twice(barge))
    assert report.readings == 2 and report.reading == {2: 2}
    assert report.failures == ()
    assert UNOBSERVABLE not in report.outcomes().values()
    voiced, unvoiced = report.results["M1"].verdicts[1:]
    assert "most_favourable_reading" in voiced.flags and "most_favourable_reading" in unvoiced.flags
    assert voiced.measures["played"] == 2 and unvoiced.measures["played"] == 0
    json.dumps(report.to_json())


def test_the_reading_that_fails_least_is_the_one_reported(barge):
    """If the voice spoke the first copy the turn went unanswered; the ledger allows either."""
    events = _generated_twice(barge)
    _forget(context(events, 2), "Stop.")
    report = check(events)
    assert report.readings == 2 and report.reading == {2: 3}
    assert report.failures == ()
    assert "most_favourable_reading" in report.results["L1"].verdicts[-1].flags


def test_a_failure_stands_when_every_reading_fails(barge):
    events = _generated_twice(barge)
    history = of(events, "history.final")["data"]["items"]
    history.append(copy.deepcopy(history[-1]))
    assert rules(check(events), "M1") == ["rule 4"]


def test_a_reading_that_is_not_causally_possible_excuses_nothing(barge):
    """The second copy's first token came after the synthesis, so the text was the first copy."""
    events = _generated_twice(barge)
    _forget(context(events, 2), "Stop.")
    text, second = of(events, "tts.synthesized", 1), of(events, "llm.response", 2)
    second["data"]["ttft_us"] = text["data"]["started_us"] - second["t_us"] + 1
    report = check(events)
    assert report.readings == 1 and report.reading == {2: 2}
    (failed,) = report.failures
    assert (failed.contract, failed.instance) == ("L1", {"speech": 1})
    assert failed.flags == ()
    # a token out one microsecond sooner, as the text is synthesized, makes it a candidate
    second["data"]["ttft_us"] -= 1
    report = check(events)
    assert report.readings == 2 and report.reading == {2: 3} and report.failures == ()


def test_a_text_no_reply_could_be_stays_unattributed(barge):
    events = _generated_twice(barge)
    text = of(events, "tts.synthesized", 1)
    for response in (of(events, "llm.response", 1), of(events, "llm.response", 2)):
        response["data"]["ttft_us"] = text["data"]["started_us"] - response["t_us"] + 1
    report = check(events)
    assert report.readings == 1 and report.reading == {}
    assert [v.outcome for v in report.results["M1"].verdicts[1:]] == [UNOBSERVABLE, UNOBSERVABLE]
    assert report.failures == ()


def _splits(report):
    return [
        (v.contract, v.instance)
        for result in report.results.values()
        for v in result.verdicts
        if "reading_split" in v.flags
    ]


def test_a_verdict_another_reading_makes_worse_is_marked(barge):
    events = _generated_twice(barge)
    agreed = check(events)
    assert agreed.readings == 2 and _splits(agreed) == []
    # The second turn is now answered only if the voice spoke the second copy.
    _forget(context(events, 2), "Stop.")
    report = check(events)
    assert report.failures == () and _splits(report) == [("L1", {"speech": 1})]
    json.dumps(report.to_json())


def _turns(*outcomes):
    """L1's results under one reading: a verdict per speech, none where ``None``."""
    made = {
        PASS: {},
        UNOBSERVABLE: {"unobservable": "the run ended before the deadline"},
        FAIL: {"findings": [Finding("milestone 1", "never happened")]},
    }
    verdicts = [
        verdict("L1", {"speech": speech}, "", flags=["silenced_by_a_tool"], **made[outcome])
        for speech, outcome in enumerate(outcomes)
        if outcome is not None
    ]
    return {"L1": ContractResult("L1", PASS, {}, tuple(verdicts))}


def test_a_split_is_a_worse_outcome_for_the_same_instance_under_another_reading():
    reported = _turns(PASS, PASS, UNOBSERVABLE, UNOBSERVABLE, FAIL, PASS)
    other = _turns(UNOBSERVABLE, FAIL, FAIL, PASS, PASS, None)
    marked = split(reported, [reported, other])["L1"].verdicts
    kept = ("silenced_by_a_tool",)  # a flag the verdict had stays, and comes first
    assert [v.flags for v in marked] == [(*kept, "reading_split")] * 3 + [kept] * 3
    assert [v.outcome for v in marked] == [v.outcome for v in reported["L1"].verdicts]


def test_an_instance_only_another_reading_judges_is_said_in_a_note():
    reported = _turns(PASS, None, None, None)
    other = _turns(PASS, FAIL, UNOBSERVABLE, PASS)
    result = split(reported, [reported, other, other])["L1"]
    assert [v.flags for v in result.verdicts] == [("silenced_by_a_tool",)]
    assert result.notes == (
        "another reading judges speech 1, which this one does not: fail",
        "another reading judges speech 2, which this one does not: unobservable",
    )
    assert split(reported, [reported])["L1"].notes == ()


def _speech(index, text, backchannel=False):
    return Speech(index, 0, 0, text, not backchannel, backchannel, ())


def _user(*texts):
    return [{"type": "message", "role": "user", "text": t} for t in texts]


def test_user_text_is_read_as_one_stream():
    first, ack, second = (
        _speech(0, "book a table"),
        _speech(1, "uh huh", True),
        _speech(2, "for six"),
    )
    merged = user_turns(_user("book a table for six"), [first, ack, second])
    assert [s.index for s in merged.present] == [0, 2] and not merged.stray
    assert user_turns(_user("book a table", "book a table"), [first]).stray == [
        "book",
        "a",
        "table",
    ]
    assert [s.index for s in user_turns(_user("for six"), [first, second]).present] == []


def test_a_backchannel_may_be_present_or_absent_even_when_the_next_turn_starts_with_its_words():
    ack, turn = _speech(0, "Stop.", True), _speech(1, "Stop. Now please.")
    dropped = user_turns(_user("Stop. Now please."), [ack, turn])
    assert [s.index for s in dropped.present] == [1] and not dropped.stray
    kept = user_turns(_user("Stop.", "Stop. Now please."), [ack, turn])
    assert [s.index for s in kept.present] == [0, 1] and not kept.stray
    # identical texts and only one kept: the turn that is owed a place is taken to be it
    same = user_turns(_user("Stop."), [ack, _speech(1, "Stop.")])
    assert [s.index for s in same.present] == [1] and not same.stray


def test_m2_fails_on_user_text_nobody_said(fixed):
    context(fixed, 2).append(
        {
            "type": "message",
            "id": "x",
            "role": "user",
            "text": "Cancel everything.",
            "interrupted": False,
        }
    )
    report = check(fixed)
    assert rules(report, "M2") == ["rule a"]
    assert report.results["M2"].failures[0].instance == {"context": "call 2"}


def test_m2_fails_when_a_transcript_disappears_from_memory(fixed):
    items = context(fixed, 2)
    items.remove(next(i for i in items if i["type"] == "message" and i["role"] == "user"))
    assert "rule b" in rules(check(fixed), "M2")


def test_m2_fails_on_an_assistant_message_that_is_no_reply(barge):
    remembered(barge, 2)["text"] = "I never said this"
    report = check(barge)
    assert rules(report, "M2") == ["rule c"]
    assert report.results["M1"].verdicts[0].outcome == UNOBSERVABLE


def shown(events, call, kind):
    return next(i for i in context(events, call) if i["type"] == kind)


def test_m2_fails_when_the_model_is_shown_a_different_result(fixed):
    shown(fixed, 2, "function_call_output")["output"] = "Reservation R9 confirmed for 2 guests."
    report = check(fixed)
    assert rules(report, "M2") == ["rule d"]
    assert "but the context shows" in report.results["M2"].failures[0].findings[0].detail


def test_m2_fails_when_a_result_comes_before_its_call(fixed):
    items = context(fixed, 2)
    call, output = shown(fixed, 2, "function_call"), shown(fixed, 2, "function_call_output")
    a, b = items.index(call), items.index(output)
    items[a], items[b] = items[b], items[a]
    assert "comes before its call" in check(fixed).results["M2"].failures[0].findings[0].detail


def test_m2_a_result_nobody_witnessed_fails_or_is_unobservable(fixed):
    ghost = {
        "type": "function_call_output",
        "id": "x",
        "name": "create_reservation",
        "output": "ok",
        "call_id": "ghost",
        "is_error": False,
    }
    invented = copy.deepcopy(fixed)
    context(invented, 2).append(ghost)
    assert rules(check(invented), "M2") == [
        "rule d"
    ]  # that tool is witnessed: the result is invented
    context(fixed, 2).append({**ghost, "name": "some_other_tool"})  # a tool Earful never saw run
    assert check(fixed).outcome("M2") == UNOBSERVABLE


def test_m2_what_the_model_is_shown_for_a_tool_that_raised_is_not_judged(fixed):
    """livekit-agents shows a tool that raised to end the turn as an empty, non-error result."""
    returned = of(fixed, "tool.returned")
    returned["kind"] = "tool.raised"
    returned["data"] = {
        "tool": 1,
        "name": "create_reservation",
        "error": "StopResponse",
        "message": "",
        "expects_reply": False,
    }
    shown(fixed, 2, "function_call_output")["output"] = ""
    assert check(copy.deepcopy(fixed)).outcome("M2") == PASS
    # older releases keep neither the call nor a result for it
    gone = copy.deepcopy(fixed)
    for kind in ("function_call", "function_call_output"):
        context(gone, 2).remove(shown(gone, 2, kind))
    assert check(gone).results["M2"].verdicts[1].outcome == PASS
    context(fixed, 2).append(dict(shown(fixed, 2, "function_call_output")))
    assert rules(check(fixed), "M2") == ["rule d"]


def test_m2_a_result_marked_as_an_error_for_a_tool_that_returned_fails(fixed):
    shown(fixed, 2, "function_call_output")["is_error"] = True
    report = check(fixed)
    assert (
        "returned normally, but its result is marked as an error"
        in report.results["M2"].failures[0].findings[0].detail
    )


def test_m2_a_cancelled_tool_call_needs_no_result(affected):
    """On the affected release the first booking's result is missing from memory."""
    assert check(copy.deepcopy(affected)).outcome("M2") == FAIL
    returned = of(affected, "tool.returned")
    returned["kind"] = "tool.cancelled"
    returned["data"] = {"tool": returned["data"]["tool"], "name": returned["data"]["name"]}
    assert check(affected).outcome("M2") == PASS


def _returns_at(events, call, *, before):
    """Move the booking's return to just before or after the request of model call ``call``."""
    returned = of(events, "tool.returned")
    events.remove(returned)
    request = next(e for e in events if e["kind"] == "llm.request" and e["data"]["call"] == call)
    returned["t_us"] = request["t_us"]
    events.insert(events.index(request) + (0 if before else 1), returned)
    return seal(events)


def test_m2_what_the_model_is_shown_for_a_call_still_running_is_not_judged(fixed):
    """livekit-agents 1.8 shows a call still running with a stand-in result."""
    _returns_at(fixed, 2, before=False)
    shown(fixed, 2, "function_call_output")["output"] = "The tool call is still in progress."
    report = check(copy.deepcopy(fixed))
    second = report.results["M2"].verdicts[1]
    assert second.outcome == PASS and second.measures["stand_in_results"] == 1
    assert report.outcome("M2") == PASS
    context(fixed, 2).append(dict(shown(fixed, 2, "function_call_output")))
    assert rules(check(fixed), "M2") == ["rule d"]


def _without_the_call(events, call):
    items = context(events, call)
    for kind in ("function_call", "function_call_output"):
        items.remove(shown(events, call, kind))
    return events


def test_m2_a_call_for_a_later_turn_made_as_the_tool_returns_is_not_held_to_it(fixed):
    """Requested at the instant the booking returned, call 2 may have been put together first."""
    assert rules(check(_without_the_call(copy.deepcopy(fixed), 2)), "M2") == ["rule d"]
    tied = _without_the_call(_returns_at(fixed, 2, before=True), 2)
    assert check(tied).results["M2"].verdicts[1].outcome == PASS


def test_m2_an_error_result_for_a_call_no_tool_received_is_the_frameworks_account(fixed):
    context(fixed, 2).append(
        {
            "type": "function_call_output",
            "id": "x",
            "name": "create_reservation",
            "output": "Error parsing arguments",
            "call_id": "bad-1",
            "is_error": True,
        }
    )
    second = check(fixed).results["M2"].verdicts[1]
    assert second.outcome == PASS and second.measures["refused_calls"] == 1


def _forget(items, text):
    items.remove(next(i for i in items if i.get("text") == text))


def test_m2_a_transcript_is_owed_once_the_turn_can_be_over(fixed):
    """The second transcript arrives at 6.2 s and its turn can be over at 7.2 s. A context
    requested before then need not hold it; the final history must."""
    early = declare(copy.deepcopy(fixed), M2={"user_commit_lag_us": 0})
    _forget(context(early, 2), "Is the table by the window?")
    assert "rule b" not in rules(check(early), "M2")
    _forget(of(fixed, "history.final")["data"]["items"], "Is the table by the window?")
    assert "rule b" in rules(check(fixed), "M2")


def test_speech_that_begins_before_a_turn_can_be_over_extends_it(fixed):
    view = View(fixed)
    first, second = view.speeches
    assert view.turn_over_us(first) == 3_700_000  # transcript at 2.7 s, plus 0.5 s + 0.5 s
    second.start = 3_500_000
    assert view.turn_over_us(first) == view.turn_over_us(second) == 7_200_000


def test_m2_fails_when_a_reply_comes_before_the_turn_it_answered(barge):
    items = context(barge, 2)
    reply = remembered(barge, 2)
    items.remove(reply)
    items.insert(
        next(n for n, i in enumerate(items) if i["type"] == "message" and i["role"] == "user"),
        reply,
    )
    assert "rule e" in rules(check(barge), "M2")


def _buffer_gap(events, at, length=0):
    paused = copy.deepcopy(of(events, "playout.paused"))
    paused["t_us"] = at
    paused["data"]["played_us"] = at - of(events, "playout.started")["t_us"]
    resumed = copy.deepcopy(paused)
    resumed.update(kind="playout.resumed", t_us=at + length)
    events.extend((paused, resumed))
    return seal(sorted(events, key=lambda event: event["t_us"]))


@pytest.mark.parametrize("gap_us", [0, 1])
def test_l2_a_zero_length_buffer_gap_does_not_start_audio_again(barge, gap_us):
    events = _buffer_gap(barge, 5_100_000, gap_us)
    report = check(events)
    assert report.valid
    assert report.outcome("L2") == (PASS if gap_us == 0 else FAIL)


def test_l2_a_zero_length_buffer_gap_does_not_hide_a_late_stop(barge):
    of(barge, "playout.paused")["t_us"] = 5_900_000
    report = check(_buffer_gap(barge, 5_500_000))
    assert report.valid and rules(report, "L2") == ["rule a"]


def _late_pause(events, by_us):
    """Shift the pause the framework issued, and what follows at the same instant."""
    at = of(events, "sink.pause")["t_us"]
    for e in events:
        if e["t_us"] == at:
            e["t_us"] = at + by_us
    return events


def test_l2_a_late_command_fails_and_puts_the_reference_in_doubt(barge):
    report = check(_late_pause(barge, 300_000))
    (failed,) = report.results["L2"].failures
    assert [f.rule for f in failed.findings] == ["rule a"]
    assert failed.flags == ("reference_in_doubt",)
    assert failed.measures["decision_lag_us"] == 300_000


def test_l2_the_stop_bound_is_a_policy_choice(barge):
    events = declare(_late_pause(barge, 300_000), L2={"stop_bound_us": 300_000})
    assert check(events).outcome("L2") == PASS


def test_l2_a_speech_shorter_than_the_threshold_has_no_interruption_point(barge):
    speech = barge[0]["data"]["scenario"]["speeches"][1]
    speech["end_us"] = speech["start_us"] + 300_000  # the framework's threshold is 500 ms
    report = check(_late_pause(seal(barge), 300_000))
    (v,) = [v for v in report.results["L2"].verdicts if v.instance == {"speech": 1}]
    assert v.outcome == PASS and v.measures["interruption_point_us"] is None


def test_l2_reports_recognition_delay_separately_from_stop_latency(barge):
    measures = check(barge).results["L2"].verdicts[1].measures
    assert measures["recognition_delay_us"] == 500_000  # true start of speech to the command
    assert measures["decision_lag_us"] == 0  # the declared threshold to the command
    assert measures["stop_latency_us"] == 0  # the command to silence


def test_l1_a_later_speech_carries_the_obligation():
    result = check(load("1.8.12", "speak_over_new_turn")).results["L1"]
    assert [v.instance for v in result.verdicts] == [{"speech": 1}]
    assert "the later speech carries the obligation" in result.notes[0]


def test_l1_reports_time_to_first_model_call_and_first_audio(barge):
    measures = check(barge).results["L1"].verdicts[0].measures
    assert measures["time_to_first_model_call_us"] == 0
    assert measures["time_to_first_audio_us"] == 500_000


def test_l1_slack_is_a_policy_choice(barge):
    assert check(declare(barge, L1={"slack_us": 0})).outcome("L1") == PASS


def test_a2_an_intent_that_shares_its_tool_and_was_never_acted_on_has_no_instance(fixed):
    fixed[0]["data"]["scenario"]["intents"].append(
        {"intent": "book-table-2", "tools": ["create_reservation"], "authorized": 1}
    )
    result = check(seal(fixed)).results["A2"]
    assert result.outcome == PASS
    assert [v.instance for v in result.verdicts] == [{"intent": INTENT}]


def test_a1_a_retired_intent_whose_tool_then_served_its_replacement_passes(fixed):
    scenario = fixed[0]["data"]["scenario"]
    scenario["intents"].append(
        {"intent": "old-booking", "tools": ["create_reservation"], "authorized": 1}
    )
    scenario["speeches"][0]["retires"] = ["old-booking"]
    report = check(seal(fixed))
    assert report.outcome("A1") == PASS and report.outcome("A2") == PASS


def _without_tool_witness(events):
    kept = [e for e in events if not e["kind"].startswith("tool.")]
    kept[0]["data"]["scenario"]["witnessed_tools"] = []
    for e in kept:
        if e["kind"] == "effect.requested":
            e["data"]["tool"] = None
    return seal(kept)


def test_an_unwitnessed_tool_is_unobservable_not_a_failure(fixed):
    report = check(_without_tool_witness(fixed))
    assert report.outcome("M2") == UNOBSERVABLE
    assert report.results["L1"].verdicts[0].outcome == UNOBSERVABLE
    assert report.failures == ()


def test_l1_fails_when_a_witnessed_tool_is_never_called(fixed):
    kept = [e for e in fixed if not e["kind"].startswith("tool.")]
    for e in kept:
        if e["kind"] == "effect.requested":
            e["data"]["tool"] = None
        if e["kind"] in ("llm.request", "history.final"):
            e["data"]["items"] = [
                i for i in e["data"]["items"] if not i["type"].startswith("function_call")
            ]
    report = check(seal(kept))
    (failed,) = report.results["L1"].failures
    assert failed.instance == {"speech": 0}
    assert "round 1: every tool call is made: never happened" in failed.findings[0].detail


def _silence_after(events, call):
    """Drop everything the agent said from model call ``call`` on, as if it was never answered."""
    utterance = next(
        e["data"]["utterance"]
        for e in events
        if e["kind"] == "tts.synthesized" and e["data"]["call"] == call
    )
    segment = next(
        e["data"]["segment"]
        for e in events
        if e["kind"] == "playout.started" and e["data"]["utterance"] == utterance
    )
    text = next(
        e["data"]["text"]
        for e in events
        if e["kind"] == "llm.response" and e["data"]["call"] == call
    )
    kept = []
    for e in events:
        d = e["data"]
        if e["kind"] == "llm.response" and d["call"] == call:
            continue
        if e["kind"] == "tts.synthesized" and d["utterance"] == utterance:
            continue
        if e["kind"].startswith(("playout.", "sink.")) and d.get("segment") == segment:
            continue
        if e["source"] == "framework":
            continue
        if e["kind"] == "history.final":
            d["items"] = [i for i in d["items"] if i.get("text") != text]
        kept.append(e)
    return seal(kept)


def test_l1_fails_when_a_model_call_is_never_answered(barge):
    report = check(_silence_after(barge, 2))
    (failed,) = report.results["L1"].failures
    assert failed.instance == {"speech": 1}
    assert "the first word of a reply starts playing: never happened" in failed.findings[0].detail


def test_l1_first_audio_counts_even_if_the_run_ends_before_the_segment_finishes(barge):
    barge.remove(of(barge, "playout.finished", 1))
    report = check(seal(barge))
    last = report.results["L1"].verdicts[-1]
    assert last.outcome == PASS and last.measures["time_to_first_audio_us"] == 800_000
    assert report.results["M1"].verdicts[-1].outcome == UNOBSERVABLE


def _in_two_segments(events, later_us=2_000_000):
    """The last reply's second word plays in a segment of its own, ``later_us`` after the first."""
    first = of(events, "playout.finished", 1)
    started, second = copy.deepcopy(of(events, "playout.started", 1)), copy.deepcopy(first)
    one, two = first["data"]["words_completed_us"]
    first["t_us"] = one
    first["data"].update(
        position_us=240_000,
        pushed_us=240_000,
        words_played=[30],
        words_completed_us=[one],
        text_played="Okay,",
    )
    started["t_us"] = one + later_us
    started["data"]["segment"] = 3
    second["t_us"] = two + later_us
    second["data"].update(
        segment=3,
        position_us=480_000,
        pushed_us=480_000,
        words_played=[31],
        words_completed_us=[two + later_us],
        text_played="stopping.",
    )
    events.extend((started, second))
    return seal(sorted(events, key=lambda event: event["t_us"]))


def test_a_reply_played_in_two_segments_was_first_audible_when_the_first_began(barge):
    """The second segment starts at 9.24 s, after the turn's deadline of 8.7 s."""
    events = _in_two_segments(barge)
    assert View(events).playout(2).first_audible_us == of(events, "playout.started", 1)["t_us"]
    report = check(events)
    assert report.valid and report.failures == ()
    assert report.results["L1"].verdicts[-1].measures["time_to_first_audio_us"] == 800_000
    assert report.results["M1"].verdicts[-1].measures["played"] == 2


def test_l2_a_speech_the_vad_never_reported_holds_no_floor(barge):
    cut = barge.index(of(barge, "user.vad_start", 1))
    result = check(seal(barge[:cut] + [barge[-1]])).results["L2"]
    assert [v.instance for v in result.verdicts] == [{"speech": 0}]
    assert "the run ended before the VAD reported it" in result.notes[0]
