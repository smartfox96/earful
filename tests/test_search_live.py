"""The boundary scheduler, the shrinker and the bundle against a real session.

A deliberate break is found by the search from a scenario that passes as written, its
window is measured, the case is reduced to the author's scenario with the one change the
failure needs, and that case replays to the very same ledger. The control, the same
scenario without the break, is searched the same way and nothing is found. The two
historical defects are found on the releases that have them and not on the releases that
repaired them.
"""

import functools
from pathlib import Path

import livekit.agents as lk
import pytest

from breaks import SEVENTH_WORD_ENDS_US
from earful.core import bundle, case as cases
from earful.core.case import Variable
from earful.core.checker import check
from earful.core.ledger import ledger_hash
from earful.core.predicate import Predicate
from earful.core.search import Runner, explore, window
from earful.core.shrink import differences, nearest, shrink
from earful.livekit import scenario
from earful.livekit.cases import CASES
from earful.livekit.harness import Delay, run_case

VERSION = tuple(int(part) for part in lk.__version__.split(".")[:3])
FIX_3702 = (1, 6, 6)
FIX_5509 = (1, 5, 7)

SLOW_TO_STOP = Predicate("L2", "rule a")
STOP = Variable("speech.position", 1)
REPLY_STARTS_US = 3_300_000  # the long reply's first audio, in every release


def late(reference):
    """A reference case with its second speech moved to long after everything, where it
    interrupts nothing: a starting point that passes on every release."""
    return cases.with_value(scenario.default(reference), STOP, 30_000_000)


_POOL = None


def side_by_side(*args):
    """What a search's ``each`` takes: one pool of worker processes for every test in this
    file. Starting a pool means starting interpreters that import the framework, and a
    pool per test was a fifth of the time these tests took."""
    global _POOL
    if _POOL is None:
        _POOL = scenario.pool(4)
    return _POOL.map(*args)


@pytest.fixture(scope="module", autouse=True)
def _workers():
    yield
    if _POOL is not None:
        _POOL.shutdown()


def interruptions(reference, **options):
    """Search a scenario by moving "Stop." so that its interruption point falls on each
    boundary. As written, the scenarios in breaks.py say "Stop." after the reply is over."""
    return explore(
        scenario.default(reference),
        scenario.run,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        faults=[],
        each=side_by_side,
        **options,
    )


# --------------------------------------------------------------------------- a case is data


def test_a_case_run_from_its_data_is_the_case_run_directly():
    for name, factory in CASES.items():
        reference = f"earful.livekit.cases:{name}"
        data = scenario.default(reference)
        assert cases.validate(data) == []
        assert scenario.describe(scenario.build(data), reference) == data
        assert ledger_hash(scenario.run(data)) == ledger_hash(run_case(factory()).ledger.events)


def test_data_that_cannot_be_run_is_refused_with_a_value_error():
    data = scenario.default("breaks:host")
    for broken, message in (
        ({**data, "settings": {**data["settings"], "volume": 11}}, "unknown setting 'volume'"),
        (
            {**data, "settings": {**data["settings"], "tts_ttfb_us": "fast"}},
            "takes a time in microseconds",
        ),
        (
            {**data, "settings": {**data["settings"], "tts_ttfb_us": -5}},
            "takes a time in microseconds",
        ),
        ({**data, "settings": {**data["settings"], "sink_can_pause": 1}}, "takes a bool"),
        ({**data, "schema": "earful.case/0"}, "not an earful.case/3 document"),
        ({**data, "scenario": "no.such.module:x"}, "no scenario 'no.such.module:x'"),
        ({**data, "scenario": "breaks:nope"}, "no scenario 'breaks:nope'"),
        (
            {
                **data,
                "delays": [
                    {"of": "llm.ttft", "nth": 1, "us": 1},
                    {"of": "llm.ttft", "nth": 1, "us": 2},
                ],
            },
            "declared more than once",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            scenario.run(broken)


def test_a_declared_delay_takes_the_place_of_what_the_code_asks_for():
    """The booking scenario's tool commits at once and its model takes 0.3 s. Declared as
    data, each delay is applied, recorded where it is applied, and stated in the header."""
    case = CASES["book_then_interrupt"]()
    case.delays = [
        Delay("backend.latency", 1, 1.5),
        Delay("llm.duration", 1, 0.7),
        Delay("llm.ttft", 2, 0.25),
        Delay("tts.ttfb", 2, 0.35),
    ]
    events = run_case(case).ledger.events
    assert events[0]["data"]["scenario"]["delays"] == [
        {"of": "backend.latency", "nth": 1, "us": 1_500_000},
        {"of": "llm.duration", "nth": 1, "us": 700_000},
        {"of": "llm.ttft", "nth": 2, "us": 250_000},
        {"of": "tts.ttfb", "nth": 2, "us": 350_000},
    ]
    of = lambda kind, n=0: [e for e in events if e["kind"] == kind][n]  # noqa: E731
    assert of("llm.response")["data"]["duration_us"] == 700_000
    assert of("llm.response", 1)["data"]["ttft_us"] == 250_000
    assert of("tts.synthesized", 1)["data"]["ttfb_us"] == 350_000
    assert of("effect.requested")["data"]["latency_us"] == 1_500_000
    assert of("effect.committed")["t_us"] - of("effect.requested")["t_us"] == 1_500_000
    assert check(events).valid
    data = scenario.describe(case, "earful.livekit.cases:book_then_interrupt")
    assert ledger_hash(scenario.run(data)) == ledger_hash(events)


def test_a_delay_that_is_no_delay_is_refused():
    case = CASES["book_then_interrupt"]()
    for bad, message in (
        (Delay("llm.speed", 1, 0.1), "unknown delay"),
        (Delay("llm.ttft", 0, 0.1), "counts from 1"),
        (Delay("llm.ttft", 1, -1), "cannot be negative"),
    ):
        case.delays = [bad]
        with pytest.raises(ValueError, match=message):
            run_case(case)


# --------------------------------------------------------------------------- the search


def test_the_scenarios_pass_as_written_before_any_search():
    for name in ("host", "slow_to_stop", "slow_to_stop_for_30ms", "slow_to_stop_on_an_instant"):
        assert scenario.default(f"breaks:{name}")["speeches"][1]["start_us"] == 14_000_000
        assert check(scenario.run(scenario.default(f"breaks:{name}"))).failures == ()
    for name in ("book_then_interrupt", "speak_over_new_turn"):
        assert check(scenario.run(late(f"earful.livekit.cases:{name}"))).failures == ()


@pytest.fixture(scope="module")
def searched():
    return interruptions("breaks:slow_to_stop")


def test_the_search_finds_the_break_from_a_scenario_that_passes(searched):
    assert searched.original.failures == ()
    assert SLOW_TO_STOP in searched.predicates() and not searched.everywhere(SLOW_TO_STOP)
    summary = searched.summary()
    assert summary["refused"] == summary["error"] == summary["invalid"] == 0
    assert summary["found"]["L2/rule a"]["changed"] == ["speech.position 1"]
    # every breaking start interrupts the reply while it plays
    assert all(
        REPLY_STARTS_US - 500_000 <= t.candidate.value_us < 11_000_000
        for t in searched.breaking(SLOW_TO_STOP)
    )
    assert (
        len(searched.breaking(SLOW_TO_STOP)) > 50
    )  # at the end of every word, and either side of it


def test_the_search_finds_nothing_in_the_control(searched):
    control = interruptions("breaks:host")
    assert SLOW_TO_STOP not in control.predicates()
    assert [t.candidate.value_us for t in control.tried] == [
        t.candidate.value_us for t in searched.tried
    ]


def test_the_window_of_a_break_is_measured_not_guessed(searched):
    """Slow to stop whenever the reply is interrupted: from the first audio to the last
    word. The window is located to a millisecond."""
    (found,) = searched.windows(SLOW_TO_STOP)
    assert found.variable == STOP and not found.instant
    assert (found.lo_end, found.hi_end) == (
        "passes",
        "passes",
    )  # a passing run was met on both sides
    # from an interruption point early in the reply to one in its last word
    assert REPLY_STARTS_US - 500_000 <= found.lo_us <= REPLY_STARTS_US
    assert found.width_us > 6_500_000 and found.resolution_us == 1_000


def test_a_break_that_lasts_30_ms_at_a_boundary_is_found_and_its_width_measured():
    """A fixed step would have to land inside 30 ms. The boundary search goes to the end
    of each word, and finds it at the boundary and 10 ms after."""
    found = interruptions("breaks:slow_to_stop_for_30ms")
    assert SLOW_TO_STOP in found.predicates()
    assert len(found.breaking(SLOW_TO_STOP)) == 2
    (measured,) = found.windows(SLOW_TO_STOP)
    opens = (
        REPLY_STARTS_US + SEVENTH_WORD_ENDS_US - 500_000
    )  # "Stop." starts 0.5 s before its interruption point
    assert (measured.lo_us, measured.hi_us) == (opens, opens + 29_000)  # located to a millisecond
    assert not measured.instant


def test_a_break_at_one_microsecond_is_an_instant_and_is_called_one():
    """It fails with the interruption exactly at the end of the word, and not a
    microsecond either side: a tie between two events, and nothing about the system."""
    found = interruptions("breaks:slow_to_stop_on_an_instant")
    assert len(found.breaking(SLOW_TO_STOP)) == 1
    (measured,) = found.windows(SLOW_TO_STOP)
    assert measured.instant and measured.width_us == 0 and measured.resolution_us == 1


def test_the_search_reaches_the_boundaries_of_a_tool_and_says_what_it_did_not_reach():
    """The booking's tool is called and commits half a second after the user stops, as the
    turn is committed. The user's next speech can be put there, because a VAD hears speech
    that resumes that soon as one; and the commit can be put anywhere after its request."""
    case = scenario.default("earful.livekit.cases:book_then_interrupt")
    found = explore(
        case,
        scenario.run,
        variables=["speech.position", "backend.latency"],
        speeches=[1],
        faults=[],
        words=False,
        each=side_by_side,
    )
    coverage = found.coverage()
    for moment in ("vad_start", "interrupt"):
        assert {"tool.called", "tool.returned", "effect.requested", "effect.committed"} <= set(
            coverage[f"speech.position 1 {moment}"]["reached"]
        )
    commit = coverage["backend.latency 1 done"]
    assert {"playout.started", "sink.pause", "user.transcript_final", "user.vad_start"} <= set(
        commit["reached"]
    )
    # what cannot be is counted and not run: the commit before its own request
    summary = found.summary()
    assert summary["impossible_placements"] > 0 and summary["refused"] == summary["error"] == 0
    assert all(d["us"] >= 0 for t in found.tried for d in t.candidate.case["delays"])


# --------------------------------------------------------------------------- reduction and replay


def test_a_found_case_reduces_to_the_authors_scenario_with_the_one_change_it_needs(
    searched, tmp_path
):
    """The case the search found, with a noise, a late remark, changed settings, a looser
    policy, a declared delay and another seed added, comes back to the scenario as written
    with "Stop." moved; and "Stop." is left well inside the window, on a round time."""
    author = scenario.default("breaks:slow_to_stop")
    found = nearest(author, [t.candidate.case for t in searched.breaking(SLOW_TO_STOP)])
    start = found["speeches"][1]["start_us"]
    extra = {"stt_delay_us": 200_000, "expects_reply": None, "backchannel": False, "retires": []}
    padded = {
        **found,
        "speeches": [
            *found["speeches"],
            {
                "start_us": start + 15_030_000,
                "end_us": start + 15_330_000,
                "transcript": "",
                **extra,
            },
            {
                "start_us": start + 19_070_000,
                "end_us": start + 19_900_000,
                "transcript": "Thanks.",
                **extra,
            },
        ],
    }
    padded["settings"] = {
        **found["settings"],
        "tts_ttfb_us": 250_000,
        "min_endpointing_delay_us": 600_000,
    }
    padded["contracts"] = {"L1": {"slack_us": 2_000_000}}
    padded["delays"] = [{"of": "llm.duration", "nth": 2, "us": 450_000}]
    padded["seed"] = 7
    assert len(differences(author, padded)) == 8

    manifest = bundle.capture(
        tmp_path / "kept",
        padded,
        scenario.run,
        SLOW_TO_STOP,
        author=author,
        environment=scenario.environment(),
        scenario_digest=scenario.fingerprint("breaks:slow_to_stop"),
    )
    kept = bundle.load(tmp_path / "kept")
    assert (manifest["original"]["speeches"], manifest["minimal"]["speeches"]) == (4, 2)
    assert (manifest["original"]["delays"], manifest["minimal"]["delays"]) == (1, 0)
    assert manifest["reduction"]["complete"]
    (difference,) = manifest["reduction"]["differences"]
    assert difference.startswith("speech 'Stop.' moved to ")
    minimal = kept.minimal
    assert {
        **minimal,
        "speeches": author["speeches"],
    } == author  # everything but the speeches is the author's
    assert minimal["speeches"][0] == author["speeches"][0]

    # "Stop." sits on a whole second, well inside the window the manifest records for it
    stop = minimal["speeches"][1]["start_us"]
    measured = next(w for w in manifest["windows"] if w["variable"] == "speech.position 1")
    assert (
        stop % 1_000_000 == 0 and measured["lo_us"] + 50_000 <= stop <= measured["hi_us"] - 50_000
    )
    assert measured["width_us"] > 6_500_000 and not measured["instant"]
    for shift in (-50_000, -1, 1, 50_000):  # and so it still fails if it moves
        assert SLOW_TO_STOP.holds(
            check(scenario.run(cases.with_value(minimal, STOP, stop + shift)))
        )

    assert manifest["replay"] == {"repeats": 3, "distinct_hashes": 1}
    assert manifest["framework"]["version"] == lk.__version__ and manifest["clock"] == "virtual"
    assert manifest["minimal"]["findings"] and manifest["original"]["findings"]
    again = bundle.replay(kept, scenario.run, repeats=5, fingerprint=scenario.fingerprint)
    assert (
        again.reproduced
        and again.repeatable
        and again.identical
        and again.scenario_changed is False
    )


def test_removing_either_speech_from_the_reduced_case_makes_it_pass(searched):
    author = scenario.default("breaks:slow_to_stop")
    found = nearest(author, [t.candidate.case for t in searched.breaking(SLOW_TO_STOP)])
    reduced = shrink(found, searched.runner, SLOW_TO_STOP, author=author)
    assert reduced.complete and len(reduced.case["speeches"]) == 2
    for index in range(2):
        assert not SLOW_TO_STOP.holds(
            check(scenario.run(cases.without_speech(reduced.case, index)))
        )


def test_a_scenario_that_fails_as_written_reduces_to_itself():
    """On every release an output that is slow to stop fails the reference barge-in as
    written. However the case was found, the reduction is the author's scenario."""
    author = scenario.default("breaks:interrupted_and_slow_to_stop")
    assert SLOW_TO_STOP.holds(check(scenario.run(author)))
    moved = cases.with_value(author, STOP, 6_130_000)
    moved["settings"]["tts_ttfb_us"] = 250_000
    reduced = shrink(moved, scenario.run, SLOW_TO_STOP, author=author)
    assert reduced.case == author and reduced.differences == []
    assert reduced.steps[0] == "kept 0 of 2 differences from the author's scenario"


def test_a_bundle_says_when_the_code_of_its_scenario_has_changed(tmp_path):
    author = scenario.default("breaks:interrupted_and_slow_to_stop")
    bundle.capture(
        tmp_path / "kept",
        author,
        scenario.run,
        SLOW_TO_STOP,
        author=author,
        scenario_digest="0" * 64,
    )
    again = bundle.replay(
        bundle.load(tmp_path / "kept"), scenario.run, repeats=1, fingerprint=scenario.fingerprint
    )
    assert again.reproduced and again.scenario_changed is True


def test_measuring_a_window_needs_a_case_that_breaks():
    with pytest.raises(ValueError, match="does not break"):
        window(Runner(scenario.run), scenario.default("breaks:host"), STOP, SLOW_TO_STOP)


# --------------------------------------------------------------------------- the historical defects


def _positions(name):
    """A reference case with its second speech moved out of the way, then searched by
    moving it back, with every one of its moments, to every boundary."""
    return explore(
        late(f"earful.livekit.cases:{name}"),
        scenario.run,
        variables=["speech.position"],
        speeches=[1],
        faults=[],
        each=side_by_side,
    )


def test_the_search_finds_the_lost_tool_result_only_where_it_exists():
    found = _positions("book_then_interrupt")
    assert found.original.failures == ()
    duplicate, lost = Predicate("A2", "rule"), Predicate("M2", "rule d")
    if VERSION < FIX_3702:
        assert {duplicate, lost} <= set(found.predicates())
        (measured,) = found.windows(duplicate, most=1)
        assert (
            measured.width_us > 4_000_000 and not measured.instant
        )  # any interruption of the reply
    else:
        assert not {duplicate, lost} & set(found.predicates())


def test_the_search_finds_the_reply_over_a_new_turn_only_where_it_exists():
    found = _positions("speak_over_new_turn")
    assert found.original.failures == ()
    over = Predicate("L2", "rule b")
    if VERSION < FIX_5509:
        assert over in found.predicates()
        assert not any(w.instant for w in found.windows(over, most=1))
    else:
        assert over not in found.predicates()


# --------------------------------------------------------------------------- the reference bundles

BUNDLES = Path(__file__).parent / "bundles"


@pytest.mark.parametrize(
    "name, repaired_in",
    [
        ("lk-1.5.6.book_then_interrupt.A2-rule", FIX_3702),
        ("lk-1.5.6.speak_over_new_turn.L2-rule-b", FIX_5509),
    ],
)
def test_a_reference_bundle_is_red_where_the_defect_is_and_green_where_it_was_repaired(
    name, repaired_in
):
    """The bundles in tests/bundles/ were made on 1.5.6. Replayed here, each fails on
    every release that has its defect and passes on every release that repaired it; on
    1.5.6 it gives the very ledger recorded, on any platform."""
    kept = bundle.load(BUNDLES / name)
    again = bundle.replay(kept, scenario.run, fingerprint=scenario.fingerprint)
    assert again.repeatable and not again.invalid and again.scenario_changed is False
    assert again.reproduced == (VERSION < repaired_in)
    assert again.identical == (lk.__version__ == kept.manifest["framework"]["version"])


# --------------------------------------------------------------------------- faults

CONFIRMED_BUT_NOT_DONE = Predicate("A3", "rule")
BOOKED_TWICE = Predicate("A2", "rule")


@functools.cache
def faults_of(reference, words=True):
    """Search a scenario by adding faults to its backend writes and moving nothing else.
    Each scenario is searched once for all the tests below. ``words=False`` leaves the
    end of every played word out of the boundaries: three quarters of the runs, and of no
    use where no rule in question depends on what had been played."""
    return explore(
        scenario.default(reference), scenario.run, variables=(), words=words, each=side_by_side
    )


def bases(found, predicate):
    """The faults under which ``predicate`` broke, without what was moved under them."""
    return sorted({tried.candidate.base for tried in found.breaking(predicate)})


def test_the_fault_scenarios_pass_as_written():
    for name in (
        "confirms_a_failed_booking",
        "reads_the_error",
        "retries_without_a_key",
        "retries_with_a_key",
    ):
        report = check(scenario.run(scenario.default(f"breaks:{name}")))
        assert report.valid and not report.failures, (name, report.failures)


def test_a_fault_search_runs_every_fault_on_every_write_and_every_run_is_judged():
    found = faults_of("breaks:confirms_a_failed_booking")
    assert {tried.candidate.base for tried in found.tried} == {
        "backend.error 1",
        "backend.drop 1",
        "backend.timeout 1, commit first",
        "backend.timeout 1, commit later",
    }
    assert all(tried.outcome.ran for tried in found.tried)
    # The moment the caller gives up, and the commit of a write that timed out, are moved.
    moved = {tried.candidate.variable.kind for tried in found.tried if tried.candidate.variable}
    assert moved == {"backend.wait", "backend.latency"}


def test_a_confirmation_of_a_write_that_failed_is_found_and_the_control_is_clean():
    found = faults_of("breaks:confirms_a_failed_booking")
    assert found.predicates() == [CONFIRMED_BUT_NOT_DONE]
    under = bases(found, CONFIRMED_BUT_NOT_DONE)
    # Nothing was written: the backend refused, or the write was lost.
    assert {"backend.error 1", "backend.drop 1"} <= set(under)
    # A timeout whose write commits before the claim is heard is not a false confirmation.
    assert "backend.timeout 1, commit later" not in under
    # The control makes no claim after an error, so word ends cannot matter to it.
    assert faults_of("breaks:reads_the_error", words=False).predicates() == []


def test_a_claim_heard_before_a_timed_out_write_commits_is_found_with_its_window():
    """The caller gives up, the model confirms anyway, and the write commits only later.
    The search moves the commit past the moment the claim is heard and measures from where
    on that fails."""
    found = faults_of("breaks:confirms_a_failed_booking")
    commit = Variable("backend.latency", 1)
    after_the_claim = [
        t for t in found.breaking(CONFIRMED_BUT_NOT_DONE) if t.candidate.variable == commit
    ]
    assert after_the_claim, "the commit was never moved past the claim"
    (measured,) = [w for w in found.windows(CONFIRMED_BUT_NOT_DONE) if w.variable == commit]
    assert measured.lo_end == "passes" and not measured.instant
    assert measured.lo_us <= min(t.candidate.value_us for t in after_the_claim)
    # One microsecond earlier the write commits as the claim's last word ends, and passes.
    edge = cases.with_value(
        after_the_claim[0].candidate.case, commit, measured.lo_us - measured.resolution_us
    )
    assert not found.runner.breaks(edge, CONFIRMED_BUT_NOT_DONE)


def test_a_retry_after_a_timeout_books_twice_without_a_key_and_once_with_one():
    # How many writes commit does not depend on what had been played: no word ends.
    found = faults_of("breaks:retries_without_a_key", words=False)
    assert BOOKED_TWICE in found.predicates()
    # Both orders of a timeout: the write had committed, or was still going to.
    assert {"backend.timeout 1, commit first", "backend.timeout 1, commit later"} <= set(
        bases(found, BOOKED_TWICE)
    )
    # A write that was refused or lost is safe to send again.
    assert not {"backend.error 1", "backend.drop 1"} & set(bases(found, BOOKED_TWICE))
    assert faults_of("breaks:retries_with_a_key", words=False).predicates() == []


def test_a_case_found_with_a_fault_reduces_to_the_fault_and_replays(tmp_path):
    found = faults_of("breaks:confirms_a_failed_booking")
    author = scenario.default("breaks:confirms_a_failed_booking")
    # A dropped write whose caller's wait the search also moved: two changes, one needed.
    moved = [
        t.candidate.case
        for t in found.breaking(CONFIRMED_BUT_NOT_DONE)
        if t.candidate.what == "backend.drop 1 + backend.wait 1"
    ]
    assert moved
    manifest = bundle.capture(
        tmp_path / "kept", moved[-1], found.runner, CONFIRMED_BUT_NOT_DONE, author=author
    )
    kept = bundle.load(tmp_path / "kept")
    assert [(f["of"], f["nth"]) for f in kept.minimal["faults"]] == [("backend.drop", 1)]
    assert manifest["minimal"]["faults"] == 1 and kept.minimal["speeches"] == author["speeches"]
    assert kept.minimal["delays"] == author["delays"]
    # Without the fault the reduced case is the author's, which passes.
    assert {**kept.minimal, "faults": []} == author
    again = bundle.replay(kept, scenario.run)
    assert again.reproduced and again.identical


def test_pairs_move_every_speech_with_each_fault_in_place():
    found = explore(
        scenario.default("earful.livekit.cases:book_then_interrupt"),
        scenario.run,
        variables=["speech.position"],
        speeches=[1],
        moments=["interrupt"],
        faults=["backend.error"],
        pairs=True,
        words=False,
        each=side_by_side,
    )
    both = [
        t
        for t in found.tried
        if t.candidate.base == "backend.error 1" and t.candidate.variable == STOP
    ]
    alone = [t for t in found.tried if not t.candidate.base]
    assert both and alone and all(t.outcome.ran for t in found.tried)
    assert all(
        t.candidate.case["faults"] == [{"of": "backend.error", "nth": 1, "us": None}] for t in both
    )
    assert {t.candidate.what for t in both} == {"backend.error 1 + speech.position 1"}


def test_profiles_are_the_same_for_the_same_seed_and_move_every_speech():
    def searched():
        return explore(
            scenario.default("earful.livekit.cases:book_then_interrupt"),
            scenario.run,
            variables=["speech.position"],
            speeches=[1],
            moments=["interrupt"],
            faults=[],
            profiles=2,
            words=False,
            each=side_by_side,
        )

    first, second = searched(), searched()
    assert [cases.canonical(t.candidate.case) for t in first.tried] == [
        cases.canonical(t.candidate.case) for t in second.tried
    ]
    under = {t.candidate.base for t in first.tried}
    assert under == {"", "profile 1", "profile 2"}
    profile = next(
        t.candidate.case
        for t in first.tried
        if t.candidate.base == "profile 1" and t.candidate.variable is None
    )
    assert profile["delays"] and all(0 <= d["us"] <= 10_000_000 for d in profile["delays"])
    assert all(t.outcome.ran for t in first.tried)


# --------------------------------------------------------------------------- hangup

LOST_RESULT = Predicate("M2", "rule d")  # livekit/agents#3702, which a hangup also reaches
HANGUP = Variable("user.hangup", 0)


@functools.cache
def hangups_of(reference):
    """Search a scenario by moving the moment the user hangs up, and nothing else. The
    ends of played words are left out of the boundaries: no assertion below needs them."""
    return explore(
        scenario.default(reference),
        scenario.run,
        variables=["user.hangup"],
        faults=[],
        words=False,
        each=side_by_side,
    )


def test_a_hangup_is_part_of_a_cases_data():
    case = CASES["barge_in_mid_word"]()
    case.speeches, case.hangup = case.speeches[:1], 5.0
    data = scenario.describe(case, "earful.livekit.cases:barge_in_mid_word")
    assert data["hangup_us"] == 5_000_000 and cases.validate(data) == []
    assert scenario.build(data).hangup == 5.0
    assert ledger_hash(scenario.run(data)) == ledger_hash(run_case(case).ledger.events)
    assert scenario.default("earful.livekit.cases:barge_in_mid_word")["hangup_us"] is None


def test_the_hangup_scenarios_pass_as_written():
    for name in ("books_again_if_the_user_left", "books_again_with_a_key"):
        report = check(scenario.run(scenario.default(f"breaks:{name}")))
        assert report.valid and not report.failures, (name, report.failures)


def test_a_hangup_search_closes_the_session_at_every_boundary_after_the_last_speech():
    found = hangups_of("breaks:books_again_with_a_key")
    author = scenario.default("breaks:books_again_with_a_key")
    assert len(found.tried) > 30 and all(t.outcome.ran for t in found.tried)
    assert all(t.candidate.variable == HANGUP for t in found.tried)
    assert min(t.candidate.value_us for t in found.tried) >= author["speeches"][-1]["end_us"]
    reached = set(found.coverage()["user.hangup 0 hangup"]["reached"])
    assert {
        "llm.request",
        "tool.called",
        "effect.requested",
        "effect.committed",
        "playout.started",
        "playout.finished",
    } <= reached
    # The control sends a key with both writes, so leaving early books once.
    assert BOOKED_TWICE not in found.predicates()
    # A hangup interrupts the reply that called the tool, which is how #3702 is reached.
    assert (LOST_RESULT in found.predicates()) == (VERSION < FIX_3702)


def test_a_booking_made_again_because_the_user_left_is_found_with_its_window():
    """The agent books again on exit unless it told the user the table is booked. The
    search finds it by moving the hangup, and measures from when to when it happens."""
    found = hangups_of("breaks:books_again_if_the_user_left")
    assert found.original.failures == ()
    assert BOOKED_TWICE in found.predicates()
    summary = found.summary()
    assert summary["found"]["A2/rule"]["changed"] == ["user.hangup 0"]
    (measured,) = found.windows(BOOKED_TWICE)
    assert measured.variable == HANGUP and not measured.instant
    assert (measured.lo_end, measured.hi_end) == ("passes", "passes")
    # From the tool being called, at about 3 s, to the end of "Your table is booked."
    assert 2_900_000 <= measured.lo_us <= 3_100_000 and measured.width_us > 6_000_000
    told = cases.with_value(
        found.breaking(BOOKED_TWICE)[0].candidate.case,
        HANGUP,
        measured.hi_us + measured.resolution_us,
    )
    assert not found.runner.breaks(told, BOOKED_TWICE)


def test_a_case_found_with_a_hangup_reduces_to_the_hangup_and_replays(tmp_path):
    found = hangups_of("breaks:books_again_if_the_user_left")
    author = scenario.default("breaks:books_again_if_the_user_left")
    case = nearest(author, [t.candidate.case for t in found.breaking(BOOKED_TWICE)])
    manifest = bundle.capture(tmp_path / "kept", case, found.runner, BOOKED_TWICE, author=author)
    kept = bundle.load(tmp_path / "kept")
    left = kept.minimal["hangup_us"]
    assert left is not None and {**kept.minimal, "hangup_us": None} == author
    (difference,) = manifest["reduction"]["differences"]
    assert difference == f"the user hangs up at {left} us"
    measured = next(w for w in manifest["windows"] if w["variable"] == "user.hangup 0")
    # Settled on a whole second, well inside the window.
    assert (
        left % 1_000_000 == 0
        and measured["lo_us"] + 1_000_000 < left < measured["hi_us"] - 1_000_000
    )
    again = bundle.replay(kept, scenario.run)
    assert again.reproduced and again.identical
