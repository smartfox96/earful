"""The example the README walks through: it fails as shipped, for the reason the README
gives, and passes with the one-line fix the README gives."""

import shutil
from pathlib import Path

import livekit.agents as lk
import pytest

import earful.livekit  # noqa: F401 - before pytest is run on a project; see test_pytest_plugin

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "booking"
AS_SHIPPED = "await self._reservations.create(party_size)"
FIXED = 'await self._reservations.create(party_size, idempotency_key=f"table-for-{party_size}")'

VERSION = tuple(int(part) for part in lk.__version__.split(".")[:3])
QUICKSTART_RELEASE = (1, 8, 12)  # the release scripts/quickstart_check.sh installs

pytestmark = pytest.mark.slow


@pytest.fixture
def example(pytester):
    for name in ("agent.py", "test_booking.py"):
        shutil.copy(EXAMPLE / name, pytester.path / name)
    return pytester


def run(example):
    return example.runpytest_inprocess(
        "-p", "no:cacheprovider", "-p", "no:asyncio", "--earful-workers", "4"
    )


def test_as_shipped_the_conversation_passes_and_the_search_finds_a_second_booking(example):
    result = run(example)
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*ContractFailure: booking on livekit-agents *contract rule(s) broken*",
            "*A2/rule  in *",
            "*2 durable effects for 'book-table'; the scenario authorized 1*",
            "*changed: backend.timeout 1*",
            "search 1 (booking) breaks A2/rule: backend.timeout 1*",
        ]
    )
    kept = example.path / ".earful" / "test_booking.py-test_every_timing_and_fault_near_it"
    assert (kept / "search-1.booking.A2-rule.case.json").exists()
    assert (kept / "search-1.booking.A2-rule.ledger.jsonl").exists()


def test_with_an_idempotency_key_sent_the_second_booking_is_gone(example):
    """The example is about one failure, and the fix removes it on every release. That
    the whole search then passes is asserted for the release the quickstart is written
    against: what else a search of this conversation meets on an older release is not
    this example's subject."""
    agent = example.path / "agent.py"
    source = agent.read_text()
    assert source.count(AS_SHIPPED) == 1
    agent.write_text(source.replace(AS_SHIPPED, FIXED))
    result = run(example)
    assert "A2/rule" not in result.stdout.str()
    if VERSION >= QUICKSTART_RELEASE:
        result.assert_outcomes(passed=2)
        assert not (example.path / ".earful").exists()
