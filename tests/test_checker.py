"""The checker on recorded and hand-edited ledgers."""

import copy
import dataclasses
import itertools
import json
import random
import re
from pathlib import Path

import pytest

from earful.core.checker import (
    FAIL,
    NOT_EXERCISED,
    PASS,
    UNOBSERVABLE,
    ContractFailure,
    PolicyError,
    check,
)
from earful.core.checker.memory import align, favourable, user_turns
from earful.core.checker.policy import POLICIES, parse, parse_all
from earful.core.checker.report import (
    RULES,
    ContractResult,
    Finding,
    allowed,
    required,
    rule_names,
    split,
    verdict,
)
from earful.core.checker.view import MAX_READINGS, Speech, Spoken, View, readings
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


CHECKER = Path(__file__).resolve().parents[1] / "src" / "earful" / "core" / "checker"


def test_the_rules_that_can_break_are_those_the_checker_names(fixed):
    assert list(RULES) == list(POLICIES) == list(check(fixed).results)
    for contract, rules in RULES.items():
        source = (CHECKER / f"{contract.lower()}.py").read_text()
        named = set(re.findall(r'Finding\(\s*"([^"]+)"', source))
        named |= set(re.findall(r'"(milestone \d)"', source))
        assert named == set(rules), contract
    assert "M2/rule d" in rule_names() and len(rule_names()) == sum(map(len, RULES.values()))
    with pytest.raises(ValueError, match="A2 has no rule 'rule z'; its rules are"):
        verdict("A2", {}, "", findings=[Finding("rule z", "")])


def test_a_rule_that_is_allowed_or_a_contract_that_is_required_has_to_exist(fixed):
    report = check(fixed)
    assert allowed(["A2/rule", "M2/rule d"]) == allowed("A2/rule") | {"M2/rule d"}
    assert required("A2") == ["A2"] and required(["L1", "A3"]) == ["L1", "A3"]
    for nothing in (["A3"], ["Z9/rule"], ["A2/rule b"], ["nonsense"], ["/rule"]):
        # Refused although nothing broke: the name is checked before the run is judged.
        with pytest.raises(ValueError, match="no contract rule .* one of: A1/rule, .*L2/rule b$"):
            report.assert_contracts(allow=nothing)
    for nothing in (["A9"], ["A2/rule"], [""]):
        with pytest.raises(ValueError, match="no contract .* are: A1, A2, A3, M1, M2, L1, L2$"):
            report.assert_contracts(require=nothing)


def test_a_required_contract_that_was_not_exercised_fails_the_assertion_and_says_why(fixed):
    report = check(fixed)
    assert report.outcome("A1") == NOT_EXERCISED and report.outcome("A2") == PASS
    report.assert_contracts(require=["A2", "A3"])
    why = "was not exercised: the run has no speech that retires an intent and whose"
    assert list(report.unjudged(["A2", "A1"])) == ["A1"] and why in report.unjudged("A1")["A1"]
    with pytest.raises(ContractFailure) as caught:
        report.assert_contracts(require=["A2", "A1"], what="the booking")
    said = str(caught.value).split("\n\n")
    assert said[0] == "the booking has no pass or fail for the required A1"
    assert said[1].startswith(f"A1 {why}") and said[2].startswith("A1  not_exercised\nA2  pass")


def test_a_required_contract_that_is_unobservable_fails_the_assertion_and_says_why(affected):
    hidden = check(_without_backend(retire(affected)))
    assert hidden.outcome("A2") == hidden.outcome("A3") == UNOBSERVABLE
    assert hidden.broken() == ["M2/rule d"]
    hidden.assert_contracts(allow=["M2/rule d"])
    with pytest.raises(ContractFailure) as caught:
        hidden.assert_contracts(require=["A2", "M2"])
    head, why, _ = str(caught.value).split("\n\n", 2)
    assert head == "the run breaks M2/rule d and has no pass or fail for the required A2"
    assert why == (
        "A2 is unobservable: a tool for the intent was called but nothing reached the backend"
    )


def test_a_required_contract_that_fails_by_an_allowed_rule_was_judged(affected):
    report = check(affected)
    assert report.outcome("A2") == FAIL
    report.assert_contracts(allow=["A2/rule", "M2/rule d"], require=["A2", "M2"])
    cut = check(affected[:-1])
    assert cut.unjudged(["A2"]) == {}
    with pytest.raises(ContractFailure, match="not valid evidence"):
        cut.assert_contracts(require=["A2"])


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
        ({"A1": {"in_flight": 1}}, "wrong kind of value"),
        ({"A1": {"reissue_allowed": [INTENT, 1]}}, "wrong kind of value"),
        ({"L1": {"slack_us": True}}, "wrong kind of value"),
        ({"M1": {"check_interrupted_flag": 1}}, "wrong kind of value"),
    ],
)
def test_a_policy_value_the_contract_does_not_define_is_refused(fixed, contracts, message):
    with pytest.raises(PolicyError, match=message):
        check(declare(fixed, **contracts))


def test_a_policy_for_a_contract_that_does_not_exist_is_refused(fixed):
    with pytest.raises(PolicyError, match=r"unknown contracts \['Z1'\]; choose from"):
        parse_all({"Z1": {}, "A1": {}})
    report = check(declare(fixed, Z1={}))
    assert not report.valid and "Z1" in report.invalid[0]


def test_a_policy_time_may_be_zero_and_may_not_be_less():
    times = [
        (contract, f.name)
        for contract, cls in POLICIES.items()
        for f in dataclasses.fields(cls)
        if isinstance(f.default, int) and not isinstance(f.default, bool)
    ]
    assert len(times) == 7
    for contract, name in times:
        assert getattr(parse(contract, {name: 0}), name) == 0
        with pytest.raises(PolicyError, match="wrong kind of value: -1"):
            parse(contract, {name: -1})


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


def _retired_beside(events, anchor, *, after):
    """The correcting transcript arrives at the instant of ``anchor``, just before or after it."""
    point = of(retire(events), "user.transcript_final", 1)
    events.remove(point)
    point["t_us"] = anchor["t_us"]
    events.insert(events.index(anchor) + after, point)
    return seal(events)


@pytest.mark.parametrize(
    "kind, after, outcome, writes, late",
    [
        ("effect.requested", False, FAIL, 1, 0),
        ("effect.requested", True, PASS, 0, 1),
        ("effect.committed", False, PASS, 0, 1),
        ("effect.committed", True, PASS, 0, 0),
    ],
)
def test_a1_a_write_at_the_instant_of_the_retirement_is_judged_by_which_came_first(
    affected, kind, after, outcome, writes, late
):
    """The second booking is requested and committed at one instant, as the correction arrives."""
    events = _retired_beside(affected, of(affected, kind, 1), after=after)
    (v,) = check(copy.deepcopy(events)).results["A1"].verdicts
    assert v.outcome == outcome
    assert (v.measures["writes_after_retirement"], v.measures["late_commits"]) == (writes, late)
    assert v.flags == (("late_commit",) if late else ())
    strict = check(declare(events, A1={"in_flight": "must_not_commit"}))
    assert rules(strict, "A1") == (["rule"] if writes else ["in-flight"] if late else [])


def _without_backend(events):
    return seal([e for e in events if not e["kind"].startswith("effect.")])


def test_an_intent_is_unobservable_when_its_tool_does_not_write_through_the_backend(affected):
    report = check(_without_backend(retire(affected)))
    assert report.outcome("A1") == UNOBSERVABLE
    assert report.outcome("A2") == UNOBSERVABLE
    assert report.outcome("A3") == UNOBSERVABLE
    reasons = {v.findings[0].detail for name in ("A2", "A3") for v in report.results[name].verdicts}
    assert reasons == {"a tool for the intent was called but nothing reached the backend"}


@pytest.mark.parametrize("after, outcome", [(False, UNOBSERVABLE), (True, PASS)])
def test_a1_a_tool_that_leaves_nothing_in_the_backend_matters_if_called_after_the_retirement(
    fixed, after, outcome
):
    """The correction arrives at the instant the tool is called, just before or just after."""
    events = _without_backend(fixed)
    events = _retired_beside(events, of(events, "tool.called"), after=after)
    assert check(events).outcome("A1") == outcome


def _moved(events, event, t_us):
    event["t_us"] = t_us
    return seal(sorted(events, key=lambda e: e["t_us"]))


@pytest.mark.parametrize("off_us, outcome", [(-1, UNOBSERVABLE), (0, UNOBSERVABLE), (1, FAIL)])
def test_a3_a_tool_first_called_after_the_claim_was_heard_cannot_have_made_it_true(
    fixed, off_us, outcome
):
    """The claim is heard at 7.92 s. A write the backend cannot see needs a call by then."""
    events = _without_backend(fixed)
    heard = of(events, "playout.finished", 1)["data"]["words_completed_us"][-1]
    events = _moved(events, of(events, "tool.returned"), heard + off_us)
    events = _moved(events, of(events, "tool.called"), heard + off_us)
    (claim,) = check(events).results["A3"].verdicts
    assert claim.outcome == outcome
    if outcome == FAIL:
        assert "never committed" in claim.findings[0].detail


def test_once_the_backend_has_a_write_for_an_intent_a_call_that_wrote_nothing_wrote_nothing(
    affected,
):
    """The second booking leaves no request: the backend's record of the intent is complete."""
    events = retire(affected)
    second = of(events, "effect.requested", 1)["data"]["request"]
    kept = [
        e
        for e in events
        if not (e["kind"].startswith("effect.") and e["data"]["request"] == second)
    ]
    assert (
        len(kept) == len(events) - 2 and len([e for e in kept if e["kind"] == "tool.called"]) == 2
    )
    report = check(seal(kept))
    assert (report.outcome("A1"), report.outcome("A2"), report.outcome("A3")) == (PASS, PASS, PASS)


def test_a3_any_commit_for_the_intent_makes_a_later_claim_true(affected):
    """The second authorized booking fails just before the claim is heard; the first stands."""
    affected[0]["data"]["scenario"]["intents"][0]["authorized"] = 2
    second = of(affected, "effect.committed", 1)
    second["kind"] = "effect.failed"
    del second["data"]["receipt"], second["data"]["count_for_intent"]
    report = check(seal(affected))
    assert report.outcome("A3") == PASS
    assert report.results["A2"].verdicts[0].measures["failed"] == 1


@pytest.mark.parametrize("authorized, outcome", [(1, FAIL), (2, PASS), (3, PASS)])
def test_a2_as_many_effects_as_were_authorized_pass_and_one_more_fails(
    affected, authorized, outcome
):
    affected[0]["data"]["scenario"]["intents"][0]["authorized"] = authorized
    (v,) = check(seal(affected)).results["A2"].verdicts
    assert v.outcome == outcome and v.measures["committed"] == 2


@pytest.mark.parametrize(
    "point, off_us, outcome",
    [
        ("span_end", -1, PASS),
        ("span_end", 0, PASS),
        ("span_end", 1, FAIL),
        ("span_start", 0, PASS),
        ("span_start", 1, FAIL),
    ],
)
def test_a3_a_commit_as_the_claim_is_heard_is_in_time_and_one_microsecond_later_is_not(
    fixed, point, off_us, outcome
):
    """The claim's first word starts at 6.9 s and its last ends at 7.92 s."""
    finished = of(fixed, "playout.finished", 1)
    ends, starts = finished["data"]["words_completed_us"][-1], of(fixed, "playout.started", 1)
    heard = ends if point == "span_end" else starts["t_us"]
    commit = of(fixed, "effect.committed")
    events = declare(_moved(fixed, commit, heard + off_us), A3={"claim_point": point})
    orders = [events]
    if off_us == 0:
        # At one instant the order in the ledger decides nothing: the rule is on the time.
        later = copy.deepcopy(events)
        moved = of(later, "effect.committed")
        later.remove(moved)
        later.insert(max(n for n, e in enumerate(later) if e["t_us"] == heard) + 1, moved)
        assert later.index(moved) > events.index(of(events, "effect.committed"))
        orders.append(seal(later))
    for ledger in orders:
        (claim,) = check(ledger).results["A3"].verdicts
        assert claim.outcome == outcome and claim.measures["claim_heard_us"] == heard
        assert claim.measures["commit_before_claim_us"] == (-off_us if outcome == PASS else None)
        if outcome == FAIL:
            assert "committed 1 us later" in claim.findings[0].detail


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


@pytest.mark.parametrize(
    "kept, drift_us, broken",
    [(8, -300_000, ["rule 2"]), (9, 0, []), (10, 0, []), (11, 420_000, ["rule 3"])],
)
def test_m1_memory_agrees_exactly_with_or_without_the_cut_word_and_no_further(
    barge, kept, drift_us, broken
):
    """Nine words played and the tenth, "then", was cut. No drift is allowed."""
    message = remembered(barge, 2)
    message["text"] = " ".join(of(barge, "llm.response")["data"]["text"].split()[:kept])
    report = check(declare(barge, M1={"ahead_us": 0, "behind_us": 0}))
    first = report.results["M1"].verdicts[0]
    assert (first.measures["played"], first.measures["cut"]) == (9, 1)
    assert first.measures["contexts"][0]["drift_us"] == drift_us
    assert [f.rule for f in first.findings] == broken


@pytest.mark.parametrize(
    "kept, policy, broken",
    [
        (12, {"ahead_us": 600_000}, []),
        (12, {"ahead_us": 599_999}, ["rule 3"]),
        (3, {"behind_us": 1_500_000}, []),
        (3, {"behind_us": 1_499_999}, ["rule 2"]),
    ],
)
def test_m1_drift_equal_to_the_tolerance_passes_and_one_microsecond_more_fails(
    barge, kept, policy, broken
):
    """Twelve words remembered is 0.6 s ahead of what played; three is 1.5 s behind."""
    message = remembered(barge, 2)
    message["text"] = " ".join(of(barge, "llm.response")["data"]["text"].split()[:kept])
    first = check(declare(barge, M1=policy)).results["M1"].verdicts[0]
    assert [f.rule for f in first.findings] == broken


def _beside(events, event, anchor, *, after):
    """Move ``event`` to the instant of ``anchor``, just before or just after it."""
    events.remove(event)
    event["t_us"] = anchor["t_us"]
    events.insert(events.index(anchor) + after, event)
    return seal(events)


@pytest.mark.parametrize("is_after, contexts", [(True, ["call 2"]), (False, ["final history"])])
def test_m1_a_context_requested_as_the_playout_finishes_judges_it_only_if_it_came_after(
    barge, is_after, contexts
):
    """Model call 2 is requested at the instant reply 1's segment finishes."""
    request = next(e for e in barge if e["kind"] == "llm.request" and e["data"]["call"] == 2)
    events = _beside(barge, request, of(barge, "playout.finished"), after=is_after)
    first = check(events).results["M1"].verdicts[0]
    assert first.outcome == PASS
    assert [row["context"] for row in first.measures["contexts"]] == contexts
    assert ("judged_on_final_history" in first.flags) == (not is_after)


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


def _never_voiced(events, call=1, segment=1):
    """The reply of model call ``call`` is given and never reaches the voice or the sink."""
    return seal(
        [
            e
            for e in events
            if not (
                (e["kind"] == "tts.synthesized" and e["data"]["call"] == call)
                or (e["kind"].startswith(("playout.", "sink.")) and e["data"]["segment"] == segment)
            )
        ]
    )


def test_m1_judges_a_reply_that_never_reached_the_output_in_every_context_after_it(barge):
    """Call 2 is shown all 29 words of a reply nobody heard; the final history has dropped it."""
    remembered(barge, 2)["text"] = of(barge, "llm.response")["data"]["text"]
    history = of(barge, "history.final")["data"]["items"]
    history.remove(next(i for i in history if i.get("role") == "assistant"))
    report = check(_never_voiced(barge))
    assert report.outcome("M2") == PASS
    first = report.results["M1"].verdicts[0]
    assert first.flags == ("never_reached_the_output", "judged_on_final_history")
    assert [row["context"] for row in first.measures["contexts"]] == ["call 2", "final history"]
    (finding,) = first.findings
    assert finding.rule == "rule 4" and "call 2: it never became audible, yet 29" in finding.detail


def test_m1_judges_a_reply_that_never_reached_the_output_on_the_final_history_if_no_call_follows(
    barge,
):
    events = _never_voiced(barge, call=2, segment=2)
    last = check(copy.deepcopy(events)).results["M1"].verdicts[-1]
    assert last.flags == ("never_reached_the_output", "judged_on_final_history")
    assert [f.rule for f in last.findings] == ["rule 4"]  # "Okay, stopping." is remembered
    _forget(of(events, "history.final")["data"]["items"], "Okay, stopping.")
    assert check(events).results["M1"].verdicts[-1].outcome == PASS


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
    one_gone = align(_said("all done"), [one, three])
    assert one_gone.matched == {"reply 3": 0} and not one_gone.ambiguous
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


def _texts(*could_be):
    """Ambiguous texts numbered from 1, each with the replies it could be, all answered."""
    calls = sorted({call for candidates in could_be for call in candidates})
    return [
        *_answered(*calls),
        *(_synthesized(n, "ambiguous", list(c)) for n, c in enumerate(could_be, 1)),
    ]


def test_texts_that_cannot_each_have_a_reply_of_their_own_are_not_read_and_cost_no_search():
    """Thirty texts for twenty-nine replies: trying every order would never finish."""
    assert readings(_texts(*[range(1, 30)] * 30)) == [{}]
    assert readings(_texts([1, 2], [])) == [{}]
    # one text has to take the reply the others cannot do without
    assert readings(_texts([1, 2], *[range(2, 12)] * 10)) == [{}]  # 10! readings: too many
    forced = readings(_texts([3, 1], [3, 2], [3]))
    assert forced == [{1: 1, 2: 2, 3: 3}]


def test_a_ledger_is_read_every_way_up_to_512_readings_and_no_further():
    def chain(texts):
        """Text n could be reply n or n + 1: one more reading than there are texts."""
        return _texts(*([n, n + 1] for n in range(1, texts + 1)))

    assert MAX_READINGS == 512
    assert len(readings(chain(510))) == 511
    assert len(readings(chain(511))) == 512
    assert readings(chain(512)) == [{}]
    assert len(readings(_texts(*([2 * n, 2 * n + 1] for n in range(9))))) == 512
    assert readings(_texts(*([2 * n, 2 * n + 1] for n in range(10)))) == [{}]


def test_readings_are_every_assignment_in_the_order_of_the_candidates():
    rng = random.Random(7)
    for _ in range(300):
        replies = range(1, rng.randint(1, 6) + 1)
        could_be = [
            rng.sample(replies, rng.randint(0, len(replies))) for _ in range(rng.randint(0, 5))
        ]
        every = [
            dict(enumerate(choice, 1))
            for choice in itertools.product(*could_be)
            if len(set(choice)) == len(choice)
        ]
        assert readings(_texts(*could_be)) == (every if 0 < len(every) <= 512 else [{}])


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


def _best_reading(texts, delivered, i=0, at=0):
    """What ``user_turns`` must find, tried every way: (stray, -turns, -found, taken)."""
    if i == len(delivered):
        return (len(texts) - at, 0, 0, ())
    speech, options = delivered[i], []
    if speech.words and texts[at : at + len(speech.words)] == speech.words:
        stray, turns, found, taken = _best_reading(texts, delivered, i + 1, at + len(speech.words))
        options.append((stray, turns - (not speech.backchannel), found - 1, (i, *taken)))
    if speech.backchannel or not speech.words:
        options.append(_best_reading(texts, delivered, i + 1, at))
    return min(options, default=(len(texts) - at, 0, 0, ()))


def test_user_turns_finds_the_best_reading_of_any_stream():
    rng = random.Random(3)
    phrases = ["ok", "ok ok", "stop", "ok stop", ""]
    for _ in range(500):
        delivered = [
            _speech(n, rng.choice(phrases), backchannel=rng.random() < 0.4)
            for n in range(rng.randint(0, 6))
        ]
        said = " ".join(rng.choice(phrases) for _ in range(rng.randint(0, 5))).split()
        found = user_turns(_user(" ".join(said)), delivered)
        stray, _turns, _found, taken = _best_reading(said, delivered)
        assert [s.index for s in found.present] == list(taken)
        assert len(found.stray) == stray


def test_user_turns_reads_a_long_conversation():
    """A reading by recursion ran out of stack near 1 200 delivered speeches."""
    delivered = [_speech(n, f"turn {n}") for n in range(5000)]
    found = user_turns(_user(*(s.transcript for s in delivered)), delivered)
    assert len(found.present) == 5000 and not found.stray
    assert found.ended_at[4999] == 4999


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


def _returns_at(events, call, *, before, booking=0):
    """Move a booking's return to just before or after the request of model call ``call``."""
    returned = of(events, "tool.returned", booking)
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


@pytest.mark.parametrize("start_us, over_us", [(3_699_999, 7_200_000), (3_700_000, 3_700_000)])
def test_speech_that_begins_at_the_instant_a_turn_can_be_over_does_not_extend_it(
    fixed, start_us, over_us
):
    view = View(fixed)
    first, second = view.speeches
    second.start = start_us
    assert view.turn_over_us(first) == over_us and view.turn_over_us(second) == 7_200_000


def test_m2_fails_when_a_reply_comes_before_the_turn_it_answered(barge):
    items = context(barge, 2)
    reply = remembered(barge, 2)
    items.remove(reply)
    items.insert(
        next(n for n, i in enumerate(items) if i["type"] == "message" and i["role"] == "user"),
        reply,
    )
    assert "rule e" in rules(check(barge), "M2")


def _spoke_again(events, start_us, text="Thanks.", backchannel=False):
    """A further speech, owed no reply, whose turn is the last item of the final history."""
    speeches = events[0]["data"]["scenario"]["speeches"]
    said = copy.deepcopy(speeches[1])
    shift = start_us - said["start_us"]
    said.update(
        speech=len(speeches),
        start_us=start_us,
        end_us=said["end_us"] + shift,
        transcript=text,
        expects_reply=False,
        backchannel=backchannel,
    )
    for kind in ("user.vad_start", "user.transcript_final", "user.vad_end"):
        event = copy.deepcopy(
            next(e for e in events if e["kind"] == kind and e["data"]["speech"] == 1)
        )
        event["t_us"] += shift
        event["data"]["speech"] = said["speech"]
        if kind == "user.transcript_final":
            event["data"]["text"] = text
        events.append(event)
    speeches.append(said)
    of(events, "history.final")["data"]["items"].append(
        {"type": "message", "id": "x", "role": "user", "text": text, "interrupted": False}
    )
    return seal(sorted(events, key=lambda event: event["t_us"]))


@pytest.mark.parametrize("after_us, late", [(0, False), (1, True), (1_280_000, True)])
def test_m2_fails_when_a_reply_is_recorded_after_a_turn_that_began_once_it_had_played(
    barge, after_us, late
):
    """Reply 2 finishes playing at 7.72 s; the history puts it after a turn begun later."""
    finished = of(barge, "playout.finished", 1)["t_us"]
    events = _spoke_again(barge, finished + after_us)
    assert check(copy.deepcopy(events)).failures == ()
    items = of(events, "history.final")["data"]["items"]
    items[-2], items[-1] = items[-1], items[-2]
    assert [i.get("text") for i in items[-2:]] == ["Thanks.", "Okay, stopping."]
    report = check(events)
    assert rules(report, "M2") == (["rule e"] if late else [])
    if late:
        (finding,) = report.results["M2"].failures[0].findings
        assert f"after speech 2, which began {after_us} us after it finished" in finding.detail


def test_m2_a_turn_that_a_backchannel_could_be_has_no_certain_place(barge):
    """One "Okay." is kept before reply 2. It was said twice: as a backchannel while reply 2
    played, and as a turn at 9.0 s."""
    events = _spoke_again(barge, 6_600_000, "Okay.", backchannel=True)
    items = of(events, "history.final")["data"]["items"]
    kept = items.pop()
    events = _spoke_again(events, 9_000_000, "Okay.")
    items.pop()
    items.insert(-1, kept)
    assert [i.get("text") for i in items[-3:]] == ["Stop.", "Okay.", "Okay, stopping."]
    assert check(copy.deepcopy(events)).outcome("M2") == PASS
    # with the backchannel's own text there as well, the second is the turn, and is misplaced
    items.insert(-1, dict(kept))
    assert rules(check(events), "M2") == ["rule e"]


def test_m2_a_reply_inside_a_turn_that_began_in_one_message_and_ended_in_another_is_misplaced(
    barge,
):
    events = _spoke_again(barge, 9_000_000, "Thanks a lot.")
    items = of(events, "history.final")["data"]["items"]
    whole = items.pop()
    items.insert(-1, {**whole, "text": "Thanks"})
    items.append({**whole, "text": "a lot."})
    assert [i.get("text") for i in items[-3:]] == ["Thanks", "Okay, stopping.", "a lot."]
    assert rules(check(events), "M2") == ["rule e"]


def test_m2_a_reply_with_a_segment_still_open_has_no_place_it_is_held_to(barge):
    """Reply 2's first segment finishes at 7.24 s; its second is open when the run ends."""
    events = _in_two_segments(barge)
    events.remove(of(events, "playout.finished", 2))
    events = _spoke_again(events, 7_500_000)
    items = of(events, "history.final")["data"]["items"]
    items[-2], items[-1] = items[-1], items[-2]
    report = check(events)
    assert report.results["M1"].verdicts[-1].outcome == UNOBSERVABLE
    assert report.outcome("M2") == PASS


def test_m2_a_turn_begins_when_the_speech_segment_holding_it_does(barge):
    """ "Thanks." at 7.9 s follows "Wait." by 0.3 s: one segment, begun as reply 2 played."""
    events = _spoke_again(_spoke_again(barge, 6_600_000, "Wait."), 7_900_000)
    for kind, speech in (("user.vad_end", 2), ("user.vad_start", 3)):
        events.remove(
            next(e for e in events if e["kind"] == kind and e["data"]["speech"] == speech)
        )
    items = of(events, "history.final")["data"]["items"]
    items.append(items.pop(-3))
    assert [i.get("text") for i in items[-3:]] == ["Wait.", "Thanks.", "Okay, stopping."]
    report = check(seal(events))
    assert report.valid and report.outcome("M2") == PASS


def _tool_items(events, call=2):
    """The booking's call item and result item, each with the items that hold it: those of a
    context, then those of the final history."""
    return [
        (items, next(i for i in items if i["type"] == kind))
        for items in (context(events, call), of(events, "history.final")["data"]["items"])
        for kind in ("function_call", "function_call_output")
    ]


def _before_the_first_turn(items, item):
    items.remove(item)
    first = next(n for n, i in enumerate(items) if i["type"] == "message" and i["role"] == "user")
    items.insert(first, item)


@pytest.mark.parametrize(
    "kind, change, said",
    [
        ("function_call", {"name": "delete_all_reservations"}, "shows the call 'call-1-1' as"),
        ("function_call", {"arguments": '{"party_size": 60}'}, "shows the call 'call-1-1' as"),
        ("function_call", {"arguments": '{"party_size":6}'}, None),
        ("function_call", {"arguments": "party_size=6"}, "shows the call 'call-1-1' as"),
        ("function_call_output", {"name": "delete_all_reservations"}, "is a result named"),
        ("function_call_output", {"name": None}, None),
    ],
)
def test_m2_fails_when_a_tool_item_is_not_the_call_as_the_model_made_it(fixed, kind, change, said):
    shown(fixed, 2, kind).update(change)
    report = check(fixed)
    assert rules(report, "M2") == (["rule f"] if said else [])
    if said:
        (failed,) = report.results["M2"].failures
        assert failed.instance == {"context": "call 2"} and said in failed.findings[0].detail
        assert of(fixed, "llm.response")["seq"] in failed.evidence


def test_m2_fails_on_a_call_item_for_a_call_the_model_did_not_make(fixed):
    invented = {**shown(fixed, 2, "function_call"), "call_id": "call-9-1"}
    context(fixed, 2).append(invented)
    report = check(fixed)
    assert rules(report, "M2") == ["rule f"]
    assert "a call the model had not made" in report.results["M2"].failures[0].findings[0].detail


@pytest.mark.parametrize("before_the_turn", [False, True])
def test_m2_a_call_the_model_makes_later_is_no_call_it_had_made(fixed, before_the_turn):
    """Call 1 asks for the booking; a context requested before it answers cannot hold the call.
    Where the item sits in that context is not judged."""
    items = context(fixed, 1)
    turn = next(n for n, i in enumerate(items) if i.get("role") == "user")
    items.insert(turn if before_the_turn else len(items), dict(shown(fixed, 2, "function_call")))
    (failed,) = check(fixed).results["M2"].failures
    assert failed.instance == {"context": "call 1"}
    assert [f.rule for f in failed.findings] == ["rule f"]


def test_m2_fails_when_a_tool_call_comes_before_the_turn_it_was_made_for(fixed):
    for items, item in _tool_items(fixed):
        _before_the_first_turn(items, item)
    report = check(fixed)
    assert rules(report, "M2") == ["rule e"]
    assert [v.instance["context"] for v in report.results["M2"].failures] == [
        "call 2",
        "final history",
    ]
    details = [f.detail for f in report.results["M2"].failures[0].findings]
    said = "of create_reservation ('call-1-1') appears before speech 0, which the model was shown"
    assert details == [
        f"item 2, the call {said} when it asked",
        f"item 3, the result {said} when it asked",
    ]


def test_m2_a_tool_call_renamed_given_other_arguments_and_moved_fails_in_every_context(fixed):
    assert check(copy.deepcopy(fixed)).outcome("M2") == PASS
    for items, item in _tool_items(fixed):
        if item["type"] == "function_call":
            item.update(name="delete_all_reservations", arguments='{"all": true}')
        _before_the_first_turn(items, item)
    report = check(fixed)
    assert rules(report, "M2") == ["rule e", "rule f"]
    assert [v.instance["context"] for v in report.results["M2"].failures] == [
        "call 2",
        "final history",
    ]


def _judged(report, label):
    (verdict,) = [v for v in report.results["M2"].verdicts if v.instance == {"context": label}]
    return verdict


def _broken(report, label):
    return [f.rule for f in _judged(report, label).findings]


@pytest.mark.parametrize(
    "lag_us, late_us, lost",
    [(0, 0, False), (0, 1, True), (None, 1_000_000, False), (None, 1_000_001, True)],
)
def test_m2_a_transcript_is_lost_if_missing_more_than_user_commit_lag_after_its_turn_is_over(
    fixed, lag_us, late_us, lost
):
    """The turn of speech 1 can be over at 7.2 s. The final history is read ``late_us`` later
    and does not hold its transcript. The default lag is one second."""
    if lag_us is not None:
        declare(fixed, M2={"user_commit_lag_us": lag_us})
    history = of(fixed, "history.final")
    _forget(history["data"]["items"], "Is the table by the window?")
    report = check(_moved(fixed, history, 7_200_000 + late_us))
    assert rules(report, "M2") == (["rule b"] if lost else [])
    if lost:
        (finding,) = _judged(report, "final history").findings
        assert f"the turn of speech 1 was over {late_us} us before this context" in finding.detail


@pytest.mark.parametrize("before, owed", [(True, ["rule d"]), (False, [])])
def test_m2_the_call_that_follows_up_a_tool_is_held_to_a_return_at_its_instant_if_it_came_first(
    affected, before, owed
):
    """Model call 3 follows up the second booking in the turn that made it, and shows neither
    the call nor its result. The booking returns at the instant call 3 is requested."""
    events = _without_the_call(_returns_at(affected, 3, before=before, booking=1), 3)
    findings = _judged(check(events), "call 3").findings
    assert [f.rule for f in findings if "create_reservation (call-2-1)" in f.detail] == owed


@pytest.mark.parametrize("early_us, owed", [(0, []), (1, ["rule d"])])
def test_m2_a_call_for_a_later_turn_is_held_to_a_tool_that_returned_a_microsecond_before_it(
    fixed, early_us, owed
):
    """Model call 2 answers speech 1 at 6.5 s and shows nothing of the booking made before."""
    events = _moved(fixed, of(fixed, "tool.returned"), 6_500_000 - early_us)
    assert _broken(check(_without_the_call(events, 2)), "call 2") == owed


@pytest.mark.parametrize("kind", ["function_call", "function_call_output"])
@pytest.mark.parametrize("copies", [0, 1, 2])
def test_m2_a_tool_call_that_returned_is_shown_with_one_call_item_and_one_result_item(
    fixed, kind, copies
):
    items = context(fixed, 2)
    at = items.index(shown(fixed, 2, kind))
    items[at : at + 1] = [dict(items[at]) for _ in range(copies)]
    report = check(fixed)
    assert _broken(report, "call 2") == ([] if copies == 1 else ["rule d"])
    if copies != 1:
        calls, results = (copies, 1) if kind == "function_call" else (1, copies)
        said = f"which holds {calls} call item(s) and {results} result item(s) for it"
        assert _judged(report, "call 2").findings[0].detail.endswith(said)


@pytest.mark.parametrize("after", [False, True])
def test_m2_a_result_shown_at_the_instant_its_tool_is_called_is_judged_by_which_came_first(
    fixed, after
):
    """The booking is called and returns at the instant model call 2 is requested."""
    request = next(e for e in fixed if e["kind"] == "llm.request" and e["data"]["call"] == 2)
    kinds = ("tool.called", "effect.requested", "effect.committed", "tool.returned")
    for kind in reversed(kinds) if after else kinds:
        _beside(fixed, of(fixed, kind), request, after=after)
    assert [e["kind"] for e in fixed if e["kind"] in kinds] == list(kinds)
    report = check(fixed)
    judged = _judged(report, "call 2")
    assert _broken(report, "call 2") == (["rule d"] if after else [])
    assert judged.measures["stand_in_results"] == 0
    if after:
        assert "a call no witnessed tool had received" in judged.findings[0].detail


def test_m2_a_reply_inside_the_turn_it_answered_comes_before_the_end_of_that_turn(fixed):
    """Reply 1 answers "Book a table for six tonight." and is recorded between its halves."""
    assert _broken(check(copy.deepcopy(fixed)), "call 2") == []
    items = context(fixed, 2)
    turn, reply = items[2], remembered(fixed, 2)
    assert (turn["text"], items.index(reply)) == ("Book a table for six tonight.", 3)
    items[2:4] = [{**turn, "text": "Book a table"}, reply, {**turn, "text": "for six tonight."}]
    (finding,) = _judged(check(fixed), "call 2").findings
    assert finding.rule == "rule e"
    assert "the reply of call 1 appears before speech 0, which it" in finding.detail


def test_m2_a_reply_is_no_message_of_the_context_it_was_generated_from(barge):
    """Model call 1 answers at the instant it is requested; its context came first."""
    context(barge, 1).append(dict(remembered(barge, 2)))
    report = check(barge)
    assert _broken(report, "call 1") == ["rule c"] and rules(report, "M2") == ["rule c"]


def _by_time(events):
    return seal(sorted(events, key=lambda event: event["t_us"]))


def _gap(events, at, length=0, *, asked=False, segment=1):
    """A segment is silent from ``at`` for ``length``: its buffer ran empty or, if ``asked``,
    the output was told to pause and then to resume."""
    paused = copy.deepcopy(of(events, "playout.paused"))
    paused["t_us"] = at
    started = of(events, "playout.started", segment - 1)["t_us"]
    paused["data"].update(segment=segment, played_us=at - started)
    resumed = copy.deepcopy(paused)
    resumed.update(kind="playout.resumed", t_us=at + length)
    added = [paused, resumed]
    if asked:
        pause, resume = (copy.deepcopy(of(events, kind)) for kind in ("sink.pause", "sink.resume"))
        pause["t_us"], resume["t_us"] = at, at + length
        resume["data"]["segment"] = 1
        added = [pause, paused, resume, resumed]
    events.extend(added)
    return _by_time(events)


@pytest.mark.parametrize("gap_us", [0, 1, 300_000])
def test_l2_a_reply_that_plays_on_after_a_gap_in_its_buffer_does_not_start(barge, gap_us):
    """The gap opens at 5.1 s, in the floor window. The reply is paused at the point, 5.5 s."""
    report = check(_gap(barge, 5_100_000, gap_us))
    assert report.valid and report.outcome("L2") == PASS


def test_l2_a_reply_that_plays_on_after_a_later_gap_was_not_asked_to_pause(barge):
    """The output was paused at 5.5 s and resumed at 6.2 s. Reply 2's buffer is empty from
    7.3 s to 7.4 s, while the user says "Thanks."."""
    events = declare(_spoke_again(barge, 7_100_000), L2={"interruptions_allowed": False})
    report = check(_gap(events, 7_300_000, 100_000, segment=2))
    assert report.valid and report.outcome("L2") == PASS


def _late_pause(events, by_us):
    """Shift the pause the framework issued, and what follows at the same instant."""
    at = of(events, "sink.pause")["t_us"]
    for e in events:
        if e["t_us"] == at:
            e["t_us"] = at + by_us
    return events


@pytest.mark.parametrize("late_us, broken", [(100_000, []), (100_001, ["rule a"])])
def test_l2_a_reply_playing_on_after_a_gap_across_the_interruption_point_is_held_to_the_bound(
    barge, late_us, broken
):
    """Its buffer is empty from 5.45 s to 5.55 s, and the point is at 5.5 s."""
    report = check(_gap(_late_pause(barge, late_us), 5_450_000, 100_000))
    assert report.valid and rules(report, "L2") == broken
    assert report.results["L2"].verdicts[1].measures["audible_after_point_us"] == late_us


@pytest.mark.parametrize("back_us, broken", [(5_999_999, ["rule a"]), (6_000_000, [])])
def test_l2_a_gap_from_before_the_point_to_the_end_of_the_floor_leaves_the_agent_silent(
    barge, back_us, broken
):
    """The framework never stops the reply. Its buffer is empty from 5.3 s, and audio is back
    as the user stops speaking at 6.0 s, or 1 us before."""
    asked = [of(barge, "sink.pause"), of(barge, "playout.paused")]
    events = _gap(barge, 5_300_000, back_us - 5_300_000)
    report = check(seal([e for e in events if e not in asked]))
    assert report.valid and rules(report, "L2") == broken


@pytest.mark.parametrize(
    "at_us, grace_us, broken",
    [
        (5_800_000, 0, ["rule b"]),
        (5_800_000, 749_999, ["rule b"]),
        (5_800_000, 750_000, []),
        (5_999_999, 0, ["rule b"]),
        (6_000_000, 0, []),
    ],
)
def test_l2_a_reply_the_framework_resumes_inside_the_floor_window_talks_over_the_user(
    barge, at_us, grace_us, broken
):
    """The floor window is open from 5.05 s to 6.0 s, and both ends are outside it."""
    resume = copy.deepcopy(of(barge, "sink.resume"))
    resumed = copy.deepcopy(of(barge, "playout.paused"))
    resume["t_us"] = resumed["t_us"] = at_us
    resume["data"]["segment"], resumed["kind"] = 1, "playout.resumed"
    barge.extend((resume, resumed))
    report = check(declare(_by_time(barge), L2={"start_grace_us": grace_us}))
    assert report.valid and rules(report, "L2") == broken
    if broken:
        (finding,) = report.results["L2"].failures[0].findings
        assert f"segment 1 resumed {at_us - 5_050_000} us after" in finding.detail


def test_l2_a_pause_of_zero_length_neither_resumes_a_reply_nor_ends_it(barge):
    assert check(_gap(copy.deepcopy(barge), 5_100_000, 0, asked=True)).outcome("L2") == PASS
    assert rules(check(_gap(copy.deepcopy(barge), 5_100_000, 1, asked=True)), "L2") == ["rule b"]
    late = check(_gap(_late_pause(barge, 400_000), 5_500_000, 0, asked=True))
    assert late.valid and rules(late, "L2") == ["rule a"]


def test_l2_a_late_command_fails_and_puts_the_reference_in_doubt(barge):
    report = check(_late_pause(barge, 300_000))
    (failed,) = report.results["L2"].failures
    assert [f.rule for f in failed.findings] == ["rule a"]
    assert failed.flags == ("reference_in_doubt",)
    assert failed.measures["decision_lag_us"] == 300_000


@pytest.mark.parametrize("late_us, broken", [(100_000, []), (100_001, ["rule a"])])
def test_l2_a_stop_at_the_bound_passes_and_one_microsecond_later_fails(barge, late_us, broken):
    report = check(_late_pause(barge, late_us))
    assert rules(report, "L2") == broken
    assert report.results["L2"].verdicts[1].measures["audible_after_point_us"] == late_us


@pytest.mark.parametrize(
    "command_us, doubt", [(5_600_000, ()), (5_600_001, ("reference_in_doubt",))]
)
def test_l2_the_reference_is_in_doubt_only_if_the_command_came_later_than_the_bound(
    barge, command_us, doubt
):
    """The output plays on for 0.2 s after it is told to pause."""
    of(barge, "sink.pause")["t_us"] = command_us
    of(barge, "playout.paused")["t_us"] = command_us + 200_000
    (failed,) = check(_by_time(barge)).results["L2"].failures
    assert [f.rule for f in failed.findings] == ["rule a"] and failed.flags == doubt
    assert failed.measures["stop_latency_us"] == 200_000


def test_l2_the_stop_bound_is_a_policy_choice(barge):
    events = declare(_late_pause(barge, 300_000), L2={"stop_bound_us": 300_000})
    assert check(events).outcome("L2") == PASS


@pytest.mark.parametrize("early_us, after_us", [(0, 0), (1, None)])
def test_l2_a_reply_that_stops_at_the_interruption_point_was_audible_at_it(
    barge, early_us, after_us
):
    report = check(_late_pause(barge, -early_us))
    assert report.valid and report.outcome("L2") == PASS
    assert report.results["L2"].verdicts[1].measures.get("audible_after_point_us") == after_us


@pytest.mark.parametrize("before_us, broken", [(0, ["rule b"]), (1, ["rule a", "rule b"])])
def test_l2_audio_that_starts_at_the_interruption_point_starts_and_was_not_under_way(
    barge, before_us, broken
):
    """Reply 2 starts at 7.0 s. A third speech reaches its interruption point then, or 1 us on."""
    report = check(_spoke_again(barge, 6_500_000 + before_us))
    last = report.results["L2"].verdicts[2]
    assert report.valid and last.measures["interruption_point_us"] == 7_000_000 + before_us
    assert [f.rule for f in last.findings] == broken


@pytest.mark.parametrize("length_us, point_us", [(500_000, 5_500_000), (499_999, None)])
def test_l2_a_speech_as_long_as_the_threshold_has_its_interruption_point_at_its_end(
    barge, length_us, point_us
):
    speech = barge[0]["data"]["scenario"]["speeches"][1]
    speech["end_us"] = speech["start_us"] + length_us
    report = check(_late_pause(seal(barge), 300_000))
    (v,) = [v for v in report.results["L2"].verdicts if v.instance == {"speech": 1}]
    assert v.measures["interruption_point_us"] == point_us
    assert [f.rule for f in v.findings] == (["rule a"] if point_us else [])


def test_l2_reports_recognition_delay_separately_from_stop_latency(barge):
    measures = check(barge).results["L2"].verdicts[1].measures
    assert measures["recognition_delay_us"] == 500_000  # true start of speech to the command
    assert measures["decision_lag_us"] == 0  # the declared threshold to the command
    assert measures["stop_latency_us"] == 0  # the command to silence


@pytest.mark.parametrize(
    "early_us, delays", [(500_000, (None, 0)), (500_001, (4_499_999, 1_200_000))]
)
def test_l2_a_command_belongs_to_the_speech_that_had_begun_when_it_reached_the_sink(
    barge, early_us, delays
):
    """The pause reaches the sink as speech 1 starts at 5.0 s, or 1 us before. The next
    command is the clear at 6.2 s."""
    verdicts = check(_by_time(_late_pause(barge, -early_us))).results["L2"].verdicts
    assert tuple(v.measures["recognition_delay_us"] for v in verdicts) == delays


def test_l1_a_later_speech_carries_the_obligation():
    result = check(load("1.8.12", "speak_over_new_turn")).results["L1"]
    assert [v.instance for v in result.verdicts] == [{"speech": 1}]
    assert "the later speech carries the obligation" in result.notes[0]


def _over_at_once(events):
    """No end-of-turn delay and no slack: a turn's first deadline is the start of its clock."""
    scenario = events[0]["data"]["scenario"]
    scenario["session"]["min_endpointing_delay_us"] = 0
    scenario["providers"]["vad"]["min_silence_us"] = 0
    return declare(events, L1={"slack_us": 0})


def _spoke_after_the_turn(events, start_us, **said):
    """Speech 1 ends at 6.0 s and its clock starts at 6.2 s, with nothing to spare: the model
    call at 6.5 s is late unless the speech at ``start_us`` excuses it."""
    speeches = events[0]["data"]["scenario"]["speeches"]
    later = {**speeches[1], "speech": 2, "start_us": start_us, "end_us": start_us + 50_000}
    speeches.append({**later, **said})
    return check(_over_at_once(events)).results["L1"]


def _noise_after_the_turn(events, start_us, timeout_us):
    events[0]["data"]["scenario"]["session"]["false_interruption_timeout_us"] = timeout_us
    (_, turn) = _spoke_after_the_turn(events, start_us, transcript="", expects_reply=False).verdicts
    first = turn.measures["milestones"][0]
    return turn.outcome, first["deadline_us"]


@pytest.mark.parametrize(
    ("start_us", "timeout_us", "expected"),
    [
        (6_050_000, 2_000_000, (PASS, 8_100_000)),  # the noise ends at 6.1 s
        (6_050_000, 300_000, (FAIL, 6_400_000)),
        (6_050_000, 0, (FAIL, 6_200_000)),
        (6_050_000, None, (FAIL, 6_200_000)),  # Pipecat declares none: it resumes no reply
        (6_199_999, 2_000_000, (PASS, 8_249_999)),
        (6_200_000, 2_000_000, (FAIL, 6_200_000)),  # noise at the deadline is after it
    ],
)
def test_l1_a_turns_clock_restarts_after_noise_by_the_false_interruption_timeout(
    barge, start_us, timeout_us, expected
):
    assert _noise_after_the_turn(barge, start_us, timeout_us) == expected


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
    """The last reply's words after its first play in a segment of their own, ``later_us`` on."""
    first = of(events, "playout.finished", 1)
    started, second = copy.deepcopy(of(events, "playout.started", 1)), copy.deepcopy(first)
    played, text = first["data"], first["data"]["text_played"].split()
    one = played["words_completed_us"][0]
    head = one - started["t_us"]
    second["t_us"] += later_us
    second["data"].update(
        segment=3,
        position_us=played["position_us"] - head,
        pushed_us=played["pushed_us"] - head,
        words_played=played["words_played"][1:],
        words_completed_us=[at + later_us for at in played["words_completed_us"][1:]],
        text_played=" ".join(text[1:]),
    )
    first["t_us"] = one
    played.update(
        position_us=head,
        pushed_us=head,
        words_played=played["words_played"][:1],
        words_completed_us=[one],
        text_played=text[0],
    )
    started["t_us"] = one + later_us
    started["data"]["segment"] = 3
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


def test_a_reply_whose_last_segment_never_finished_is_not_judged_on_the_part_that_did(fixed):
    """Three of the claim's four words are in a segment still open when the run ends."""
    whole = check(_in_two_segments(copy.deepcopy(fixed)))
    assert (whole.outcome("A3"), whole.outcome("M1")) == (PASS, PASS)
    events = _in_two_segments(fixed)
    events.remove(of(events, "playout.finished", 2))
    view = View(seal(events))
    assert view.playout(2).pending and len(view.playout(2).segments) == 1
    report = check(events)
    (claim,) = report.results["A3"].verdicts
    assert claim.outcome == UNOBSERVABLE and report.results["A3"].notes == ()
    assert "before its playout finished" in claim.findings[0].detail
    last = report.results["M1"].verdicts[-1]
    assert last.outcome == UNOBSERVABLE
    assert "before its playout finished" in last.findings[0].detail


def test_m1_a_reply_whose_audio_may_be_in_a_segment_that_never_started_is_unobservable(barge):
    """The sink was handed a second segment and the run ended before it became audible."""
    barge.remove(of(barge, "playout.started", 1))
    barge.remove(of(barge, "playout.finished", 1))
    events = seal(barge)
    assert View(events).open_segments() == [2]
    report = check(events)
    assert report.valid and report.results["M1"].verdicts[-1].outcome == UNOBSERVABLE
    assert rules(report, "M1") == []


def test_l2_a_speech_the_vad_never_reported_holds_no_floor(barge):
    cut = barge.index(of(barge, "user.vad_start", 1))
    result = check(seal(barge[:cut] + [barge[-1]])).results["L2"]
    assert [v.instance for v in result.verdicts] == [{"speech": 0}]
    assert result.notes == ("speech 1: the VAD never reported it, so it held no floor",)


def test_l2_a_speech_the_vad_did_not_report_in_a_run_that_went_on_holds_no_floor(barge):
    """A speech shorter than the VAD's minimum is never reported, though the run continues."""
    unreported = [
        e
        for e in barge
        if not (e["kind"] in ("user.vad_start", "user.vad_end") and e["data"]["speech"] == 1)
    ]
    result = check(seal(unreported)).results["L2"]
    assert [v.instance for v in result.verdicts] == [{"speech": 0}]
    assert result.notes == ("speech 1: the VAD never reported it, so it held no floor",)


SEEN = "a model call sees the turn"
MADE = "round 1: every tool call is made"
FINISHES = "round 1: every tool call finishes"
FOLLOWS = "round 1: a further model call follows"
AUDIO = "the first word of a reply starts playing"


def _nothing_to_spare(events, **policy):
    """The second turn of the affected booking, with its deadlines where its milestones are."""
    scenario = events[0]["data"]["scenario"]
    scenario["providers"]["vad"]["min_silence_us"] = 0
    scenario["session"]["min_endpointing_delay_us"] = 300_000
    of(events, "tts.synthesized", 1)["data"]["ttfb_us"] = 17_647
    return declare(events, L1={"slack_us": 0, **policy})


def _second_turn(events):
    (turn,) = [v for v in check(events).results["L1"].verdicts if v.instance == {"speech": 1}]
    return turn


def _steps(turn):
    return [
        (m["milestone"], m["reached_us"], m["deadline_us"]) for m in turn.measures["milestones"]
    ]


def _missed(turn):
    assert turn.outcome == FAIL
    return [(f.rule, f.detail) for f in turn.findings]


def _moved_together(events, moved, t_us):
    for event in moved:
        event["t_us"] = t_us
    return _by_time(events)


def test_l1_a_deadline_adds_the_delays_declared_on_the_chain_to_the_end_of_the_turn(affected):
    """The clock starts at 6.2 s and the turn can be over 0.3 s later. Reply 2 takes 0.3 s to
    generate, 17 647 us to its first audio and 5.16 s to play."""
    turn = _second_turn(_nothing_to_spare(affected))
    assert turn.outcome == PASS
    assert _steps(turn) == [
        (SEEN, 6_500_000, 6_500_000),
        (MADE, 6_800_000, 6_800_000),
        (FINISHES, 6_800_000, 11_977_647),
        (FOLLOWS, 11_977_647, 11_977_647),
        (AUDIO, 6_817_647, 6_817_647),
    ]


_REACHED_BY = {
    SEEN: ("milestone 1", [("llm.request", 1), ("llm.response", 1)]),
    MADE: (
        "milestone 2",
        [
            ("tool.called", 1),
            ("effect.requested", 1),
            ("effect.committed", 1),
            ("tool.returned", 1),
        ],
    ),
    FINISHES: ("milestone 2", [("tool.returned", 1)]),
    FOLLOWS: ("milestone 2", [("llm.request", 2), ("llm.response", 2)]),
    AUDIO: ("milestone 3", [("playout.started", 1)]),
}


@pytest.mark.parametrize("late_us", [0, 1])
@pytest.mark.parametrize("step", list(_REACHED_BY))
def test_l1_a_milestone_reached_at_its_deadline_is_met_and_one_microsecond_later_is_missed(
    affected, step, late_us
):
    events = _nothing_to_spare(affected)
    deadline = {name: due for name, _, due in _steps(_second_turn(events))}[step]
    rule, reached_by = _REACHED_BY[step]
    reached = [of(events, kind, n) for kind, n in reached_by]
    turn = _second_turn(_moved_together(events, reached, deadline + late_us))
    if late_us:
        said = f"{step}: happened 1 us after its deadline (deadline {deadline} us)"
        assert _missed(turn) == [(rule, said)]
    else:
        assert turn.outcome == PASS and (step, deadline, deadline) in _steps(turn)


def _second_tool(events, called_us):
    """A second tool call in the round of model call 2, made at ``called_us`` and done at once."""
    asked = of(events, "llm.response", 1)["data"]["tool_calls"]
    asked.append({**asked[0], "call_id": "call-2-2"})
    called, returned = (
        copy.deepcopy(of(events, kind, 1)) for kind in ("tool.called", "tool.returned")
    )
    called["data"].update(tool=3, call_id="call-2-2")
    returned["data"]["tool"] = 3
    events = _moved_together([*events, called, returned], [called, returned], called_us)
    return events, returned


@pytest.mark.parametrize(
    "called_us, returned_us, after_the_call, missed",
    [
        (6_800_000, 11_977_647, False, None),
        (6_800_001, 6_800_001, False, (MADE, "happened 1 us after its deadline", 6_800_000)),
        (6_800_000, 11_977_648, True, (FINISHES, "happened 1 us after its deadline", 11_977_647)),
        (6_800_000, 11_977_647, True, (FOLLOWS, "never happened", 11_977_647)),
    ],
)
def test_l1_a_round_of_two_tool_calls_is_held_to_the_later_of_them(
    affected, called_us, returned_us, after_the_call, missed
):
    """Model call 3 is requested at 11 977 647 us, the deadline of the round. The second tool's
    return comes before that request in the ledger, or after it."""
    events, returned = _second_tool(_nothing_to_spare(affected), called_us)
    request = next(e for e in events if e["kind"] == "llm.request" and e["data"]["call"] == 3)
    if returned_us == request["t_us"]:
        events = _beside(events, returned, request, after=after_the_call)
    else:
        events = _moved(events, returned, returned_us)
    assert (events.index(returned) > events.index(request)) == after_the_call
    turn = _second_turn(events)
    if missed is None:
        assert turn.outcome == PASS and (FINISHES, returned_us, returned_us) in _steps(turn)
    else:
        step, how, deadline = missed
        assert _missed(turn) == [("milestone 2", f"{step}: {how} (deadline {deadline} us)")]


@pytest.mark.parametrize("outcome", [None, "tool.cancelled"])
def test_l1_a_round_finishes_only_when_every_one_of_its_tool_calls_has(affected, outcome):
    """Of two tool calls, the second never returns, or is cancelled."""
    events, returned = _second_tool(_nothing_to_spare(affected), 6_800_000)
    if outcome is None:
        events.remove(returned)
    else:
        returned.update(kind=outcome, data={"tool": 3, "name": returned["data"]["name"]})
    said = f"{FINISHES}: never happened (deadline 11977647 us)"
    assert _missed(_second_turn(seal(events))) == [("milestone 2", said)]


@pytest.mark.parametrize("late_us", [0, 1])
def test_l1_a_call_the_framework_refused_is_made_when_the_model_finished_asking(affected, late_us):
    """No tool receives the second booking, and model call 3 is shown an error result for it.
    Call 2 answers at 6.5 s and takes 0.3 s."""
    events = _nothing_to_spare(affected)
    for kind in ("tool.called", "effect.requested", "effect.committed", "tool.returned"):
        events.remove(of(events, kind, 1))
    shown(events, 3, "function_call_output")["is_error"] = True
    turn = _second_turn(_moved(events, of(events, "llm.response", 1), 6_500_000 + late_us))
    if late_us:
        said = f"{MADE}: happened 1 us after its deadline (deadline 6800000 us)"
        assert _missed(turn) == [("milestone 2", said)]
    else:
        assert turn.outcome == PASS
        assert _steps(turn)[1:4] == [
            (MADE, 6_800_000, 6_800_000),
            (FINISHES, 6_800_000, 11_977_647),
            (FOLLOWS, 11_977_647, 11_977_647),
        ]


@pytest.mark.parametrize("max_steps, followed", [(0, []), (1, [MADE, FINISHES, FOLLOWS])])
def test_l1_follows_as_many_rounds_of_tool_calls_as_max_steps_and_no_more(
    affected, max_steps, followed
):
    turn = _second_turn(_nothing_to_spare(affected, max_steps=max_steps))
    assert turn.outcome == PASS
    assert [name for name, _, _ in _steps(turn)] == [SEEN, *followed, AUDIO]


@pytest.mark.parametrize("slack_us, late_us", [(5_077_647, 0), (5_077_646, 1)])
def test_l1_a_reply_generated_again_is_budgeted_from_the_chain_as_it_stood_then(
    affected, slack_us, late_us
):
    """With no round followed, model call 3 is a second call for the turn, made after the 0.3 s
    call 2 took. Reply 2 is never voiced. Reply 3 takes 0.3 s and 0.2 s to its first audio."""
    events = _nothing_to_spare(affected, max_steps=0, slack_us=slack_us)
    turn = _second_turn(_never_voiced(events, call=2, segment=2))
    assert _steps(turn)[-1] == (AUDIO, 12_377_647, 12_377_647 - late_us)
    assert turn.outcome == (FAIL if late_us else PASS)


@pytest.mark.parametrize("after_us, outcome", [(-1, UNOBSERVABLE), (1, FAIL)])
def test_l1_a_milestone_never_reached_is_missed_only_if_the_run_went_on_past_its_deadline(
    affected, after_us, outcome
):
    """Neither reply of the turn is voiced. The first word is due by 12.66 s, the later of the
    two budgets, and the final history is read a microsecond before then or after."""
    events = _nothing_to_spare(affected)
    for call in (2, 3):
        events = _never_voiced(events, call=call, segment=call)
    events = _moved(events, of(events, "history.final"), 12_660_000 + after_us)
    turn = _second_turn(events)
    assert turn.outcome == outcome and _steps(turn)[-1] == (AUDIO, None, 12_660_000)


@pytest.mark.parametrize("off_us, clock_us", [(-1, 6_000_000), (0, 6_000_000), (1, 6_000_001)])
def test_l1_the_clock_starts_at_the_later_of_the_end_of_the_speech_and_its_transcript(
    barge, off_us, clock_us
):
    """Speech 1 ends at 6.0 s."""
    events = _moved(barge, of(barge, "user.transcript_final", 1), 6_000_000 + off_us)
    turn = _second_turn(events)
    assert turn.measures["clock_start_us"] == clock_us
    assert _steps(turn)[0] == (SEEN, 6_500_000, clock_us + 2_000_000)


@pytest.mark.parametrize("start_us, carried", [(6_199_999, True), (6_200_000, False)])
def test_l1_a_speech_that_starts_at_the_deadline_does_not_carry_the_obligation(
    barge, start_us, carried
):
    result = _spoke_after_the_turn(barge, start_us)
    if carried:
        assert [v.instance for v in result.verdicts] == [{"speech": 0}]
        (note,) = result.notes
        assert note.startswith("speech 1: the user spoke again before a response was due;")
    else:
        said = f"{SEEN}: happened 300000 us after its deadline (deadline 6200000 us)"
        assert _missed(result.verdicts[1]) == [("milestone 1", said)] and not result.notes


@pytest.mark.parametrize("left_us, excused", [(6_199_999, True), (6_200_000, False)])
def test_l1_a_user_who_hangs_up_at_the_deadline_was_owed_the_milestone(barge, left_us, excused):
    """The model call for speech 1 is due at 6.2 s and is made at 6.5 s."""
    barge[0]["data"]["scenario"]["hangup_us"] = left_us
    hangup = {**of(barge, "user.origin"), "kind": "user.hangup", "data": {}}
    result = check(_moved([*_over_at_once(barge), hangup], hangup, left_us)).results["L1"]
    if excused:
        assert [v.instance for v in result.verdicts] == [{"speech": 0}]
        assert result.notes == ("speech 1: the user hung up before a response was due",)
    else:
        said = f"{SEEN}: happened 300000 us after its deadline (deadline 6200000 us)"
        assert _missed(result.verdicts[1]) == [("milestone 1", said)] and not result.notes


@pytest.mark.parametrize("after", [False, True])
def test_a_transcript_delivered_as_a_model_call_is_requested_is_in_its_context_if_it_came_first(
    barge, after
):
    """Speech 0's transcript arrives at the instant model call 1 is requested."""
    transcript, request = of(barge, "user.transcript_final"), of(barge, "llm.request")
    report = check(_beside(barge, transcript, request, after=after))
    first = report.results["L1"].verdicts[0]
    if after:
        assert [v.instance for v in report.results["M2"].failures] == [{"context": "call 1"}]
        assert rules(report, "M2") == ["rule a"]
        said = f"{SEEN}: never happened (deadline 4700000 us)"
        assert _missed(first) == [("milestone 1", said)]
    else:
        assert report.outcome("M2") == PASS and first.outcome == PASS
