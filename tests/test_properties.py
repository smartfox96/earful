"""Properties of the time conversion, the shrinker and the checker, on generated inputs."""

import copy
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from earful.core import case as cases
from earful.core.case import Variable
from earful.core.checker import FAIL, NOT_EXERCISED, PASS, UNOBSERVABLE, check
from earful.core.checker.report import RULES
from earful.core.ledger import SOURCES, read_jsonl, run_id
from earful.core.predicate import Predicate
from earful.core.schema import validity
from earful.core.search import Outcome, Runner, Window, window
from earful.core.shrink import GRIDS_US, MARGIN_US, applied, ddmin, differences, settled, shrink
from earful.core.timebase import to_seconds, to_us

# Derandomized, with no example database: a run tries the same examples as the one before.
settings.register_profile(
    "earful",
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("earful")

TESTS = Path(__file__).parent
A2 = Predicate("A2", "rule")


@example(2_010_000)  # 2.01 s is 2009999.9999999998 us as a float product
@given(st.integers(-(10**13), 10**13))
def test_a_time_an_author_writes_in_seconds_is_that_many_microseconds(us):
    written = f"{'-' if us < 0 else ''}{abs(us) // 10**6}.{abs(us) % 10**6:06d}"
    assert to_us(float(written)) == us
    assert to_us(to_seconds(us)) == us


@given(st.data())
def test_ddmin_gives_a_failing_sub_list_that_passes_without_any_one_of_its_items(data):
    items = list(range(data.draw(st.integers(1, 12))))
    failing = data.draw(st.sets(st.frozensets(st.sampled_from(items)), max_size=8))
    tried = []

    def fails(kept):
        tried.append(kept)
        return len(kept) == len(items) or frozenset(kept) in failing

    found = ddmin(items, fails)
    assert len(tried) <= len(items) ** 2 + 3 * len(items) + 2  # ddmin's worst case
    assert fails(found) and found == sorted(set(found))
    assert not any(fails(found[:i] + found[i + 1 :]) for i in range(len(found)))


@given(st.integers(1, 40).flatmap(lambda n: st.tuples(st.just(n), st.sets(st.integers(0, n - 1)))))
def test_ddmin_finds_exactly_the_items_a_failure_needs(drawn):
    count, needed = drawn
    assert ddmin(range(count), lambda kept: needed <= set(kept)) == sorted(needed)


SAID = ("Book a table for six tonight.", "Is the table by the window?", "Thanks.", "Stop.", "")
SCENARIO = "some.module:scenario"


def speech(start_us, end_us, transcript, **changes):
    return {
        "start_us": start_us,
        "end_us": end_us,
        "transcript": transcript,
        "stt_delay_us": 200_000,
        "expects_reply": None,
        "backchannel": False,
        "retires": [],
        **changes,
    }


@st.composite
def a_case(draw, transcripts=None):
    """A valid case. Without ``transcripts`` a text may be said more than once."""
    if transcripts is None:
        transcripts = draw(st.lists(st.sampled_from(SAID), max_size=4))
    speeches, at = [], 0
    for transcript in transcripts:
        start = at + draw(st.integers(0, 4_000_000))
        at = start + draw(st.integers(1, 3_000_000))
        speeches.append(
            speech(
                start,
                at,
                transcript,
                stt_delay_us=draw(st.sampled_from([0, 200_000, 350_000])),
                expects_reply=draw(st.sampled_from([None, True, False])),
                backchannel=draw(st.booleans()),
                retires=draw(st.lists(st.sampled_from(["book-table-1", "cancel-1"]), max_size=1)),
            )
        )
    delays = draw(
        st.dictionaries(
            st.tuples(st.sampled_from(cases.DELAY_KINDS), st.integers(1, 2)),
            st.integers(0, 5_000_000),
            max_size=3,
        )
    )
    faults = draw(
        st.dictionaries(st.integers(1, 3), st.sampled_from(cases.FAULT_KINDS), max_size=2)
    )
    stays = not speeches or draw(st.booleans())
    return {
        "schema": cases.SCHEMA,
        "scenario": SCENARIO,
        "seed": draw(st.integers(0, 3)),
        "speeches": speeches,
        "hangup_us": None if stays else at + draw(st.integers(0, 5_000_000)),
        "delays": [{"of": of, "nth": nth, "us": us} for (of, nth), us in sorted(delays.items())],
        "faults": sorted(
            (
                {
                    "of": of,
                    "nth": nth,
                    "us": None if of == "backend.error" else draw(st.integers(0, 3_000_000)),
                }
                for nth, of in faults.items()
            ),
            key=lambda f: (f["of"], f["nth"]),
        ),
        "settings": draw(
            st.dictionaries(
                st.sampled_from(["tts_ttfb_us", "sink_can_pause", "min_silence_us"]),
                st.sampled_from([True, False, 200_000, 250_000]),
            )
        ),
        "contracts": draw(
            st.dictionaries(
                st.sampled_from(["L1", "M1"]),
                st.sampled_from([{"slack_us": 2_000_000}, {"ahead_us": 0}, {}]),
            )
        ),
    }


@given(a_case(), a_case())
def test_the_differences_between_two_cases_turn_one_into_the_other(author, found):
    assert cases.validate(author) == [] == cases.validate(found)
    between = differences(author, found)
    assert applied(author, between) == found
    assert (between == []) == (author == found)
    assert len({d.target for d in between}) == len(between)
    assert applied(author, []) == author and differences(author, author) == []


class Decided(Runner):
    """A runner whose outcomes come straight from a function of the case, with no ledger."""

    def __init__(self, fails):
        super().__init__(lambda case: [])
        self._fails, self._known = fails, {}

    def seen(self, case):
        return cases.canonical(case) in self._known

    def outcome(self, case):
        key = cases.canonical(case)
        if key not in self._known:
            self.runs += 1
            self._known[key] = Outcome(case, (A2,) if self._fails(case) else (), hash="")
        return self._known[key]


@st.composite
def a_failure(draw):
    """A case, and a failure that needs some of what it holds and one speech within a window."""
    found = draw(a_case(draw(st.lists(st.sampled_from(SAID[:4]), unique=True, max_size=4))))
    said = [s["transcript"] for s in found["speeches"]]
    needed = draw(st.sets(st.sampled_from(said))) if said else set()
    declared = [(d["of"], d["nth"]) for d in found["delays"]]
    delays = draw(st.sets(st.sampled_from(declared))) if declared else set()
    faulted = [f["nth"] for f in found["faults"]]
    faults = draw(st.sets(st.sampled_from(faulted))) if faulted else set()
    leaves = found["hangup_us"] is not None and draw(st.booleans())
    timed = draw(st.sampled_from(sorted(needed))) if needed and draw(st.booleans()) else None
    lo = hi = None
    if timed is not None:
        start = next(s["start_us"] for s in found["speeches"] if s["transcript"] == timed)
        lo = max(0, start - draw(st.integers(0, 3_000_000)))
        hi = start + draw(st.integers(0, 3_000_000))

    def fails(case):
        starts = {s["transcript"]: s["start_us"] for s in case["speeches"]}
        return (
            needed <= starts.keys()
            and delays <= {(d["of"], d["nth"]) for d in case["delays"]}
            and faults <= {f["nth"] for f in case["faults"]}
            and (case["hangup_us"] is not None or not leaves)
            and (timed is None or lo <= starts[timed] <= hi)
        )

    return found, fails, (needed, delays, faults, leaves)


@settings(max_examples=30)
@given(a_failure(), st.none() | a_case())
def test_a_reduced_case_still_fails_and_holds_nothing_the_failure_does_not_need(failure, author):
    found, fails, (needed, delays, faults, leaves) = failure
    runner = Decided(fails)
    shrunk = shrink(found, runner, A2, author=author)
    case = shrunk.case
    assert cases.validate(case) == [] and fails(case)
    assert shrunk.complete and shrunk.runs == runner.runs
    assert {(d["of"], d["nth"]) for d in case["delays"]} == delays
    assert {f["nth"] for f in case["faults"]} == faults
    assert (case["hangup_us"] is not None) == leaves
    kept = [s["transcript"] for s in case["speeches"]]
    # A user who hangs up has spoken: one speech stays where the failure needs the hangup alone.
    assert set(kept) == needed or (leaves and not needed and len(kept) == 1)
    again = shrink(found, Decided(fails), A2, author=author)
    assert (again.case, again.steps, again.runs) == (case, shrunk.steps, shrunk.runs)


@given(a_failure(), st.none() | a_case(), st.integers(1, 60))
def test_a_reduction_out_of_runs_returns_a_case_that_fails(failure, author, budget):
    found, fails, _ = failure
    runner = Decided(fails)
    shrunk = shrink(found, runner, A2, author=author, max_runs=budget)
    assert cases.validate(shrunk.case) == [] and fails(shrunk.case)
    assert shrunk.runs == runner.runs <= budget and shrunk.complete == (shrunk.runs < budget)


@pytest.mark.slow
@settings(max_examples=500)
@given(a_failure(), st.none() | a_case(), st.integers(1, 60))
def test_reductions_keep_both_properties_on_five_hundred_more_failures(failure, author, budget):
    whole = test_a_reduced_case_still_fails_and_holds_nothing_the_failure_does_not_need
    whole.hypothesis.inner_test(failure, author)
    test_a_reduction_out_of_runs_returns_a_case_that_fails.hypothesis.inner_test(
        failure, author, budget
    )


FIRST = Variable("speech.position", 0)


@st.composite
def a_window(draw):
    """One speech, perhaps with another after it, and the starts at which a failure holds."""
    start = draw(st.integers(0, 6_000_000))
    length = draw(st.integers(1, 2_000_000))
    case = draw(a_case([]))
    case["speeches"] = [speech(start, start + length, SAID[0])]
    latest = None  # the last start at which the speech is over before the next begins
    if draw(st.booleans()):
        latest = start + draw(st.integers(0, 4_000_000))
        case["speeches"].append(speech(latest + length, latest + length + 1, SAID[1]))
    near, far = st.integers(0, 3_000), st.integers(0, 5_000_000)
    lo, hi = start - draw(near | far), start + draw(near | far)
    return case, lo, hi, latest


@given(a_window())
def test_a_window_ends_within_its_resolution_of_where_the_failure_does(drawn):
    case, lo, hi, latest = drawn
    found = window(Decided(lambda c: lo <= c["speeches"][0]["start_us"] <= hi), case, FIRST, A2)
    first, last = max(lo, 0), hi if latest is None else min(hi, latest)
    assert first <= found.lo_us <= case["speeches"][0]["start_us"] <= found.hi_us <= last
    assert found.lo_us - first < found.resolution_us > last - found.hi_us
    assert found.resolution_us == 1 or (found.resolution_us == 1_000 and found.lo_us < found.hi_us)
    # An end says why probing stopped, where the two reasons are over a resolution apart.
    if lo <= 0:
        assert found.lo_end == "blocked"
    elif lo >= found.resolution_us:
        assert found.lo_end == "passes"
    if latest is not None and hi >= latest:
        assert found.hi_end == "blocked"
    elif latest is None or hi <= latest - found.resolution_us:
        assert found.hi_end == "passes"


ENDS = st.sampled_from(["passes", "blocked", "reach"])


@given(st.integers(0, 20_000_000), st.integers(1, 12_000_000), ENDS, ENDS, st.data())
def test_a_settled_time_is_inside_its_window_and_as_round_as_the_window_allows(
    lo, width, lo_end, hi_end, data
):
    hi = lo + width
    at = data.draw(st.integers(lo, hi))
    found = Window(FIRST, lo, hi, lo_end, hi_end, 1_000)
    to = settled(found, at)
    if "reach" not in (lo_end, hi_end):
        low, high, aim = lo + width // 4, hi - width // 4, (lo + hi) // 2
        assert to == settled(found, data.draw(st.integers(lo, hi)))
    elif lo_end != "reach":
        low = aim = min(lo + MARGIN_US, hi)
        high = min(low + 1_000_000, hi)
    elif hi_end != "reach":
        high = aim = max(hi - MARGIN_US, lo)
        low = max(high - 1_000_000, lo)
    else:
        low, high, aim = max(at - 500_000, lo), min(at + 500_000, hi), at
    assert lo <= low <= high <= hi
    grid = next((g for g in GRIDS_US if -(-low // g) * g <= high), None)
    if grid is None:
        assert to is None  # no whole millisecond to settle on
        return
    on_it = range(-(-low // grid) * grid, high + 1, grid)
    assert to in on_it and all(abs(to - aim) <= abs(p - aim) for p in on_it)


GOLDEN = sorted((TESTS / "golden").glob("*.jsonl"))
LEDGERS = {path.name: read_jsonl(path) for path in GOLDEN}


def seal(events):
    run = run_id(events[0]["data"])
    for seq, event in enumerate(events):
        event["seq"], event["run"] = seq, run
    return events


def _golden(release, case):
    return copy.deepcopy(LEDGERS[f"lk-{release}.{case}.ledger.normalized.jsonl"])


def _of(events, kind, n=0):
    return [e for e in events if e["kind"] == kind][n]


def _retired(release):
    """The second speech retires the booking: before the write on 1.8.12, after it on 1.5.6."""
    events = _golden(release, "book_then_interrupt")
    events[0]["data"]["scenario"]["speeches"][1]["retires"] = ["book-table-1"]
    return seal(events)


def _claimed_before_the_commit():
    events = _golden("1.8.12", "book_then_interrupt")
    commit = _of(events, "effect.committed")
    events.remove(commit)
    commit["t_us"] = _of(events, "playout.finished", 1)["data"]["words_completed_us"][-1] + 1
    events.append(commit)
    return seal(sorted(events, key=lambda e: e["t_us"]))


def _cut_reply_forgotten():
    """The reply cut after 9 of its 29 words is missing from the next model call."""
    events = _golden("1.8.12", "barge_in_mid_word")
    request = next(e for e in events if e["kind"] == "llm.request" and e["data"]["call"] == 2)
    items = request["data"]["items"]
    items.remove(next(i for i in items if i["type"] == "message" and i["role"] == "assistant"))
    return seal(events)


def _tool_never_called():
    events = [e for e in _golden("1.8.12", "book_then_interrupt") if e["kind"][:5] != "tool."]
    for event in events:
        if event["kind"] == "effect.requested":
            event["data"]["tool"] = None
        if event["kind"] in ("llm.request", "history.final"):
            items = event["data"]["items"]
            items[:] = [i for i in items if not i["type"].startswith("function_call")]
    return seal(events)


def _outside_the_backend():
    """The booking tool writes nowhere the backend sees, and the booking is retired."""
    return seal([e for e in _retired("1.5.6") if not e["kind"].startswith("effect.")])


# Made by hand from the reference ledgers, for what none of those has: a retirement, a claim
# heard before its commit, a cut reply forgotten, a tool never called, a write outside the backend.
HAND_MADE = {
    "retired before the write": _retired("1.8.12"),
    "retired after the write": _retired("1.5.6"),
    "claimed before the commit": _claimed_before_the_commit(),
    "cut reply forgotten": _cut_reply_forgotten(),
    "tool never called": _tool_never_called(),
    "outside the backend": _outside_the_backend(),
}
INPUTS = {**LEDGERS, **HAND_MADE}
# A third each: recorded on LiveKit, recorded on Pipecat, and made by hand.
reference_ledgers = st.one_of(
    st.sampled_from(sorted(name for name in LEDGERS if name.startswith("lk-"))),
    st.sampled_from(sorted(name for name in LEDGERS if name.startswith("pc-"))),
    st.sampled_from(list(HAND_MADE)),
).map(lambda name: copy.deepcopy(INPUTS[name]))


def test_the_inputs_make_every_contract_pass_and_fail_and_lose_its_evidence():
    seen = {name: set() for name in RULES}
    for name, events in INPUTS.items():
        report = check(copy.deepcopy(events))
        assert report.valid, name
        for contract, result in report.results.items():
            seen[contract] |= {v.outcome for v in result.verdicts}
    assert {c: sorted({PASS, FAIL} - s) for c, s in seen.items() if not {PASS, FAIL} <= s} == {}
    assert all(UNOBSERVABLE in seen[c] for c in ("A1", "A2", "A3"))


def later(events, by_us, since=0):
    """The same run with its clock ``by_us`` further on from event ``since``."""
    out = copy.deepcopy(events)
    before = out[since]["t_us"] if since else -1

    def moved(t_us):
        return t_us + by_us if t_us > before else t_us

    for event in out[since:]:
        event["t_us"] += by_us
        data = event["data"]
        if event["kind"] == "tts.synthesized":
            data["started_us"] = moved(data["started_us"])
        elif event["kind"] == "playout.finished":
            data["words_completed_us"] = [moved(t) for t in data["words_completed_us"]]
    return out


@st.composite
def ledgers(draw, cut=True):
    """A reference ledger, perhaps with events left out (``cut``) and with a stall in it."""
    events = draw(reference_ledgers)
    inner = st.integers(1, len(events) - 2)
    if cut:
        ranges = st.builds(lambda a, b: set(range(a, b)), inner, inner)
        gone = draw(st.sets(inner, max_size=6) | ranges)
        events = [e for i, e in enumerate(events) if i not in gone]
    if draw(st.booleans()):
        since = draw(st.integers(1, len(events) - 1))
        events = later(events, draw(st.integers(1, 3_000_000)), since)
    return seal(events)


def decided(report, at=lambda seq: seq):
    """What a report decides, with each event it cites as ``at`` numbers it."""
    return [
        (
            v.contract,
            json.dumps(v.instance, sort_keys=True),
            v.outcome,
            [(f.rule, [at(seq) for seq in f.seqs]) for f in v.findings],
            [at(seq) for seq in v.evidence],
            v.witnesses,
            v.flags,
        )
        for result in report.results.values()
        for v in result.verdicts
    ], [result.notes for result in report.results.values()]


@settings(max_examples=25)
@given(ledgers())
def test_a_ledger_the_schema_admits_is_judged_the_same_twice_and_left_as_it_was(events):
    before = copy.deepcopy(events)
    report = check(events)
    assert events == before and check(events) == report
    reasons = validity(events)
    assert (report.valid, list(report.invalid)) == (not reasons, reasons)
    assert list(report.results) == ([] if reasons else list(RULES))
    json.dumps(report.to_json())
    for name, result in report.results.items():
        outcomes = {v.outcome for v in result.verdicts}
        worst = next((o for o in (FAIL, UNOBSERVABLE, PASS) if o in outcomes), NOT_EXERCISED)
        assert result.outcome == worst
        for v in result.verdicts:
            cited = [events[seq] for seq in (*v.evidence, *(s for f in v.findings for s in f.seqs))]
            # A verdict rests on what Earful witnessed, never on the framework's own account.
            assert {e["source"] for e in cited} <= set(SOURCES) - {"framework"}
            assert set(v.witnesses) == {events[seq]["source"] for seq in v.evidence}
            assert (v.outcome == FAIL) == any(f.rule in RULES[name] for f in v.findings)


@settings(max_examples=40)
@given(st.data())
def test_a_ledger_cut_short_is_not_valid_evidence(data):
    events = data.draw(reference_ledgers)
    report = check(events[: data.draw(st.integers(0, len(events) - 1))])
    assert not report.valid and report.invalid and report.results == {}


@settings(max_examples=40)
@given(ledgers(), st.data())
def test_the_frameworks_own_events_decide_nothing(events, data):
    own = [e["seq"] for e in events if e["source"] == "framework"]
    gone = data.draw(st.sets(st.sampled_from(own))) if own else set()
    kept = [e for e in copy.deepcopy(events) if e["seq"] not in gone]
    renumbered = {e["seq"]: seq for seq, e in enumerate(kept)}
    assert decided(check(seal(kept))) == decided(check(events), renumbered.__getitem__)


# Over a second: a speech timed from the wrong zero then falls outside its own turn.
SHIFTS = st.integers(1_000_000, 10**10)


@settings(max_examples=40)
@given(ledgers(), SHIFTS)
def test_where_the_clock_began_decides_nothing(events, by_us):
    assert decided(check(later(events, by_us))) == decided(check(events))


TOLERANCES = {
    "M1": ("ahead_us", "behind_us"),
    "M2": ("user_commit_lag_us",),
    "L1": ("slack_us",),
    "L2": ("stop_bound_us", "start_grace_us"),
}


def failed(events, contracts):
    events[0]["data"]["contracts"] = contracts
    report = check(seal(events))
    assert report.valid
    return {(v.contract, json.dumps(v.instance, sort_keys=True)) for v in report.failures}


def admitted(events):
    return not validity(events)


@settings(max_examples=25)
@given(ledgers(cut=False).filter(admitted), st.data())
def test_a_wider_tolerance_fails_nothing_a_narrower_one_passes(events, data):
    narrow, wide = {}, {}
    tolerance = st.integers(0, 3_000_000)
    for contract, names in TOLERANCES.items():
        for name in names:
            a, b = sorted(data.draw(st.tuples(tolerance, tolerance)))
            narrow.setdefault(contract, {})[name], wide.setdefault(contract, {})[name] = a, b
    assert failed(events, wide) <= failed(events, narrow)


@pytest.mark.slow
@settings(max_examples=300)
@given(ledgers(), SHIFTS, st.data())
def test_the_checker_keeps_these_properties_on_three_hundred_more_ledgers(events, by_us, data):
    judged = test_a_ledger_the_schema_admits_is_judged_the_same_twice_and_left_as_it_was
    judged.hypothesis.inner_test(events)
    test_the_frameworks_own_events_decide_nothing.hypothesis.inner_test(events, data)
    test_where_the_clock_began_decides_nothing.hypothesis.inner_test(events, by_us)
    if admitted(events):
        widened = test_a_wider_tolerance_fails_nothing_a_narrower_one_passes
        widened.hypothesis.inner_test(events, data)
