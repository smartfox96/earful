"""What a shape's run is matched against, and a run in another process (tests/shapes/corpus.py)."""

import os
import time

import pytest
from shapes import corpus
from shapes.corpus import KnownDefect, Seen, ends_within, hangs, invalid, judged, raises, refused

CLEAN = Seen("judged", outcomes={"A2": "pass", "M2": "pass", "L2": "not_exercised"})
BROKEN = Seen(
    "judged",
    outcomes={"A2": "pass", "M2": "fail", "L2": "not_exercised"},
    broken=("M2/rule b", "M2/rule c"),
)


def test_a_judged_run_matches_the_outcomes_it_names_and_nothing_else_fails():
    assert judged().matches(CLEAN) and judged(A2="pass").matches(CLEAN)
    assert not judged(L2="pass").matches(CLEAN)  # named: it has to be judged
    assert not judged().matches(BROKEN)  # unnamed: it may not fail
    assert judged(M2="fail").matches(BROKEN)
    assert judged(M2="rule b, rule c").matches(BROKEN)
    assert not judged(M2="rule c").matches(BROKEN)  # every rule it breaks, no fewer
    assert not judged(M3="pass").matches(CLEAN)
    with pytest.raises(AssertionError, match="no contract is called X1"):
        judged(X1="pass")


def test_a_run_that_ends_otherwise_matches_a_part_of_what_it_said():
    stopped = Seen("invalid", said="harness error: the pipeline ended before the scenario did")
    assert invalid("the pipeline ended").matches(stopped)
    assert not invalid("deadlocked").matches(stopped) and not refused("pipeline").matches(stopped)
    assert raises("VirtualTimeStall").matches(Seen("raised", said="VirtualTimeStall: deadlocked"))
    assert hangs().matches(Seen("hung", seconds=20)) and not hangs().matches(CLEAN)
    assert ends_within(20).matches(stopped) and not ends_within(20).matches(Seen("hung"))
    assert ends_within(20, judged()).matches(CLEAN)
    assert not ends_within(20, judged()).matches(BROKEN)


def test_a_run_passes_as_it_should_fails_as_its_defect_or_fails_outright():
    corpus.judge(judged(), None, CLEAN)
    with pytest.raises(KnownDefect, match="M2 rule b, rule c"):
        corpus.judge(judged(), judged(M2="rule b, rule c"), BROKEN)
    with pytest.raises(AssertionError) as raised:
        corpus.judge(judged(), judged(M2="rule b"), BROKEN)
    assert raised.type is AssertionError and "the defect it declares: judged: M2 rule b" in str(
        raised.value
    )


def test_a_defect_is_an_expected_failure_on_the_releases_it_names():
    (mark,) = corpus.defect_marks(judged(M2="fail"), (), "1.8.13")
    reason = "Earful: judged: M2 fail, nothing else fails"
    assert mark.kwargs == {"strict": True, "raises": KnownDefect, "reason": reason}
    assert corpus.defect_marks(judged(M2="fail"), ("1.6.6",), "1.8.13") == []
    assert corpus.defect_marks(None, (), "1.8.13") == []


def refuses(case, seed, *, require_valid):
    raise ValueError(f"refused {case}")


def a_case():
    return "a case"


def sleeps():
    time.sleep(60)


def exits():
    os._exit(5)


def test_a_run_apart_reports_what_it_showed_and_is_stopped_at_its_bound():
    adapter = "test_corpus:refuses"
    seen = corpus.observe_apart("test_corpus", "a_case", adapter, 30)
    assert (seen.ended, seen.said) == ("refused", "refused a case")
    started = time.perf_counter()
    assert corpus.observe_apart("test_corpus", "sleeps", adapter, 3) == Seen("hung", seconds=3)
    assert time.perf_counter() - started < 10
    gone = corpus.observe_apart("test_corpus", "exits", adapter, 30)
    assert gone.ended == "raised" and gone.said.startswith("the process ended with status 5")
