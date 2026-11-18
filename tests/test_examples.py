"""The README's booking example fails as shipped and passes with the README's fix."""

import re
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
    return example.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")


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


SKILL = EXAMPLE.parents[1] / "skills" / "earful" / "SKILL.md"
# An agent with the tool the skill's test names.
SUPPORT_AGENT = '''
import livekit.agents as lk


class SupportAgent(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You handle refunds.")

    @lk.function_tool
    async def issue_refund(self, order: str) -> str:
        """Refund an order.

        Args:
            order: The order number
        """
        raise AssertionError("a test reached the payment service")
'''


def test_the_test_the_skill_shows_a_coding_assistant_runs_as_written(pytester):
    """Only the conversation as written is run: what a search of it finds depends on the release."""
    (shown, *_others) = re.findall(r"```python\n(.*?)```", SKILL.read_text(), flags=re.DOTALL)
    (pytester.path / "myapp").mkdir()
    (pytester.path / "myapp" / "__init__.py").write_text("")
    (pytester.path / "myapp" / "agent.py").write_text(SUPPORT_AGENT)
    (pytester.path / "test_refund.py").write_text(shown)
    result = pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "-p", "no:asyncio", "-k", "as_written"
    )
    result.assert_outcomes(passed=1, deselected=1)


def test_with_an_idempotency_key_sent_the_second_booking_is_gone(example):
    """The fix removes A2/rule on every release. The whole search is asserted to pass only
    from the release the quickstart is written against."""
    agent = example.path / "agent.py"
    source = agent.read_text()
    assert source.count(AS_SHIPPED) == 1
    agent.write_text(source.replace(AS_SHIPPED, FIXED))
    result = run(example)
    assert "A2/rule" not in result.stdout.str()
    if VERSION >= QUICKSTART_RELEASE:
        result.assert_outcomes(passed=2)
        assert not (example.path / ".earful").exists()
