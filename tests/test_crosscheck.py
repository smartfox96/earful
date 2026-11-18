"""The comparison of a real-clock run with the virtual run of the same case."""

import copy
from pathlib import Path

import pytest

from earful.core.crosscheck import ASPECTS, Tolerance, compare, cross_check
from earful.core.ledger import read_jsonl, run_id
from earful.core.schema import validity

GOLDEN = Path(__file__).parent / "golden"


def ledger(release, name="book_then_interrupt"):
    return read_jsonl(GOLDEN / f"lk-{release}.{name}.ledger.normalized.jsonl")


VIRTUAL = ledger("1.5.6")  # breaks A2/rule and M2/rule d: livekit/agents#3702
RULES = ("A2/rule", "M2/rule d")


def renumbered(events):
    for seq, event in enumerate(events):
        event["seq"] = seq
    return events


def as_real(events, *, drift_us=3_000, change=None):
    """A copy of ``events`` marked as a real-clock run, ``drift_us`` late, altered by ``change``."""
    events = copy.deepcopy(events)
    events[0]["data"]["clock"] = "real"
    for event in events[1:]:
        event["t_us"] += drift_us
    if change is not None:
        events = renumbered(change(events) or events)
    run = run_id(events[0]["data"])
    for event in events:
        event["run"] = run
    return events


def first(events, kind, **data):
    return next(
        e for e in events if e["kind"] == kind and all(e["data"][k] == v for k, v in data.items())
    )


def aspects(comparison):
    return sorted({d.aspect for d in comparison.differences}, key=ASPECTS.index)


def test_a_real_clock_run_that_does_the_same_a_little_later_agrees():
    real = as_real(VIRTUAL)
    assert validity(real) == ["the run was on the real clock"]
    comparison = compare(VIRTUAL, real)
    assert comparison.agrees and comparison.differences == ()
    assert comparison.rules == comparison.real_rules == RULES
    assert comparison.drift_us == 0  # times are measured from the user's first audio


def test_the_drift_is_the_largest_distance_of_an_event_from_its_virtual_time():
    def late(events):
        first(events, "history.final")["t_us"] += 40_000
        events[-1]["t_us"] += 40_000

    comparison = compare(VIRTUAL, as_real(VIRTUAL, change=late))
    assert comparison.agrees and comparison.drift_us == 40_000


def test_an_event_further_from_its_virtual_time_than_the_tolerance_is_a_difference():
    def late(events):
        for event in events[first(events, "history.final")["seq"] :]:
            event["t_us"] += 150_000

    real = as_real(VIRTUAL, change=late)
    comparison = compare(VIRTUAL, real)
    assert not comparison.agrees and aspects(comparison) == ["timing"]
    assert comparison.drift_us == 150_000
    assert comparison.on("timing")[0].detail.startswith("history.final at ")
    assert compare(VIRTUAL, real, Tolerance(time_us=200_000)).agrees


def test_two_events_the_virtual_clock_puts_together_may_come_in_either_order():
    def swapped(events):
        committed = first(events, "effect.committed", request=1)["seq"]
        returned = first(events, "tool.returned", tool=1)["seq"]
        assert events[committed]["t_us"] == events[returned]["t_us"]
        events[committed], events[returned] = events[returned], events[committed]

    assert compare(VIRTUAL, as_real(VIRTUAL, change=swapped)).agrees


def test_an_event_that_comes_before_one_it_clearly_follows_is_out_of_order():
    def early(events):
        moved = events.pop(first(events, "user.vad_start", speech=1)["seq"])
        to = first(events, "user.transcript_final", speech=0)
        moved["t_us"] = to["t_us"]
        events.insert(to["seq"], moved)

    comparison = compare(VIRTUAL, as_real(VIRTUAL, change=early))
    assert aspects(comparison) == ["order", "timing"]
    said = [d.detail for d in comparison.on("order")]
    assert any(d.startswith("user.vad_start 1 came before user.transcript_final 0") for d in said)


def test_an_event_only_one_clock_has_is_named():
    def without(events):
        del events[first(events, "sink.flush")["seq"]]

    comparison = compare(VIRTUAL, as_real(VIRTUAL, change=without))
    assert [d.detail for d in comparison.on("order")] == [
        "sink.flush 1 happened on the virtual clock only"
    ]


def cut(words):
    """The interrupted first segment, with its last ``words`` played words not played."""

    def change(events):
        played = first(events, "playout.finished", segment=1)["data"]
        assert played["interrupted"]
        for name in ("words_played", "words_completed_us"):
            played[name] = played[name][:-words]
        played["text_played"] = " ".join(played["text_played"].split()[:-words])

    return change


def test_a_segment_cut_short_may_differ_by_the_declared_number_of_words():
    one_fewer = compare(VIRTUAL, as_real(VIRTUAL, change=cut(1)))
    assert one_fewer.on("audible") == ()
    three_fewer = compare(VIRTUAL, as_real(VIRTUAL, change=cut(3)))
    assert [d.detail for d in three_fewer.on("audible")] == [
        "segment 1 played 7 words; 10 on the virtual"
    ]
    assert compare(VIRTUAL, as_real(VIRTUAL, change=cut(3)), Tolerance(words=3)).on("audible") == ()
    assert compare(VIRTUAL, as_real(VIRTUAL, change=cut(1)), Tolerance(words=0)).on("audible")


def test_a_segment_that_played_out_has_to_have_played_every_word():
    def change(events):
        whole = next(
            e["data"]
            for e in events
            if e["kind"] == "playout.finished" and not e["data"]["interrupted"]
        )
        whole["words_played"] = whole["words_played"][:-1]
        whole["words_completed_us"] = whole["words_completed_us"][:-1]

    assert compare(VIRTUAL, as_real(VIRTUAL, change=change)).on("audible")


def test_a_segment_cut_short_on_one_clock_only_is_a_difference():
    def change(events):
        first(events, "playout.finished", segment=1)["data"]["interrupted"] = False

    (said,) = compare(VIRTUAL, as_real(VIRTUAL, change=change)).on("audible")
    assert said.detail == "segment 1 was not cut short, unlike on the virtual clock"


def remembered(words):
    """Every context and the final history, with the interrupted reply ``words`` shorter."""

    def change(events):
        for event in events:
            for item in event["data"].get("items", []):
                if item.get("role") == "assistant" and item.get("interrupted"):
                    item["text"] = " ".join(item["text"].split()[:-words])

    return change


def test_a_reply_cut_short_may_be_remembered_shorter_by_the_declared_words():
    assert compare(VIRTUAL, as_real(VIRTUAL, change=remembered(1))).on("memory") == ()
    differs = compare(VIRTUAL, as_real(VIRTUAL, change=remembered(3))).on("memory")
    assert differs and all("item" in d.detail for d in differs)
    assert {d.detail.split(",")[0] for d in differs} >= {"the final history"}


def test_a_context_with_another_item_or_another_user_text_is_a_difference():
    def another_item(events):
        first(events, "llm.request", call=2)["data"]["items"].pop()

    (said,) = compare(VIRTUAL, as_real(VIRTUAL, change=another_item)).on("memory")
    assert said.detail.startswith("model call 2 holds ")

    def another_text(events):
        items = first(events, "llm.request", call=2)["data"]["items"]
        next(i for i in items if i.get("role") == "user")["text"] = "Something else."

    (said,) = compare(VIRTUAL, as_real(VIRTUAL, change=another_text)).on("memory")
    assert said.detail.startswith("model call 2, item ")


def test_a_write_that_did_not_commit_on_the_real_clock_is_a_difference_and_changes_the_verdict():
    def uncommitted(events):
        del events[first(events, "effect.committed", request=2)["seq"]]

    comparison = compare(VIRTUAL, as_real(VIRTUAL, change=uncommitted))
    assert "effects" in aspects(comparison) and "verdicts" in aspects(comparison)
    (said,) = comparison.on("effects")
    assert said.detail.startswith("write 2: ") and "no outcome" in said.detail
    assert comparison.rules == RULES and "A2/rule" not in comparison.real_rules
    (verdict,) = comparison.on("verdicts")
    assert verdict.detail.endswith("the virtual run broke A2/rule, M2/rule d")


def test_runs_that_are_not_a_virtual_and_a_real_run_of_one_case_are_refused():
    real = as_real(VIRTUAL)
    with pytest.raises(ValueError, match="the virtual run is not valid"):
        compare(real, real)
    with pytest.raises(ValueError, match="not a real-clock run"):
        compare(VIRTUAL, VIRTUAL)
    with pytest.raises(ValueError, match="do not declare the same framework"):
        compare(VIRTUAL, as_real(ledger("1.8.12")))
    with pytest.raises(ValueError, match="do not declare the same case"):
        compare(VIRTUAL, as_real(ledger("1.5.6", "barge_in_mid_word")))
    with pytest.raises(ValueError, match="cannot be negative"):
        Tolerance(time_us=-1)


def test_a_real_clock_run_that_is_not_a_complete_run_for_another_reason_is_unusable():
    def underran(events):
        events[-1]["data"]["underruns"] = 2

    comparison = compare(VIRTUAL, as_real(VIRTUAL, change=underran))
    assert not comparison.agrees and comparison.differences == ()
    assert comparison.unusable == ("playout ran ahead of its audio 2 times",)
    assert comparison.rules == RULES and comparison.real_rules == ()


def test_a_real_clock_hangup_need_not_fall_on_its_declared_microsecond():
    """On the virtual clock the validator holds a hangup to its declared time exactly."""
    from earful.core.schema import validate

    hung_up = copy.deepcopy(ledger("1.8.12", "barge_in_mid_word"))
    header, origin = hung_up[0]["data"], first(hung_up, "user.origin")
    finished = max(e["t_us"] for e in hung_up if e["kind"] == "playout.finished")
    header["scenario"]["hangup_us"] = finished - origin["t_us"] + 1_000_000
    hung_up.insert(
        first(hung_up, "history.final")["seq"],
        {
            **origin,
            "kind": "user.hangup",
            "data": {},
            "t_us": origin["t_us"] + header["scenario"]["hangup_us"],
        },
    )
    run = run_id(header)
    for event in renumbered(hung_up):
        event["run"] = run
    assert validate(hung_up) == []

    def a_little_late(events):
        first(events, "user.hangup")["t_us"] += 4_000

    late = as_real(hung_up, change=a_little_late)
    assert validity(late) == ["the run was on the real clock"]
    assert compare(hung_up, late).agrees and compare(hung_up, late).drift_us == 4_000
    virtual_and_late = copy.deepcopy(hung_up)
    first(virtual_and_late, "user.hangup")["t_us"] += 4_000
    assert any("not at the declared time" in error for error in validate(virtual_and_late))


def test_a_cross_check_says_how_many_runs_agree_and_how_each_differs():
    checked = cross_check(VIRTUAL, [as_real(VIRTUAL), as_real(VIRTUAL, change=cut(3))])
    assert not checked.agrees and not checked.unusable
    assert [c.agrees for c in checked.comparisons] == [True, False]
    assert checked.lines()[:5] == [
        "book_then_interrupt on livekit-agents 1.5.6",
        "on the virtual clock it breaks A2/rule, M2/rule d",
        "1 of 2 real-clock runs agree with the virtual run, within 100 ms and 1 word(s)",
        "run 1: agrees; breaks A2/rule, M2/rule d; largest drift 0.0 ms",
        "run 2: differs; breaks A2/rule, M2/rule d; largest drift 0.0 ms",
    ]
    assert "    audible: segment 1 played 7 words; 10 on the virtual" in checked.lines()
    as_json = checked.to_json()
    assert as_json["agrees"] is False and [r["agrees"] for r in as_json["runs"]] == [True, False]
    assert cross_check(VIRTUAL, [as_real(VIRTUAL)] * 2).agrees
    assert not cross_check(VIRTUAL, []).agrees  # nothing was compared


@pytest.mark.slow
def test_a_scenario_is_run_on_both_clocks_and_the_real_run_is_never_evidence():
    """Agreement depends on the machine, so only what holds on any machine is asserted."""
    from earful.livekit.api import cross_check as on_both_clocks

    checked = on_both_clocks("earful.livekit.cases:speak_over_new_turn", runs=1)
    (real,), (comparison,) = checked.real, checked.comparisons
    assert validity(checked.virtual) == []
    assert validity(real) == ["the run was on the real clock"]
    assert not comparison.unusable and comparison.drift_us > 0
    assert checked.lines()[0].startswith("speak_over_new_turn on livekit-agents ")
    with pytest.raises(ValueError, match="at least one real-clock run"):
        on_both_clocks("earful.livekit.cases:speak_over_new_turn", runs=0)
