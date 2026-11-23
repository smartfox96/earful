"""The Pipecat booking example fails as shipped and passes as its walkthrough ends."""

from __future__ import annotations

import re
import shutil
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("pipecat")

from shapes.corpus import KnownDefect

import earful.pipecat  # noqa: F401 - before pytest is run on a project, as in test_examples

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "pipecat_booking"
PAGE = (ROOT / "docs" / "pipecat.md").read_text()
AS_SHIPPED = "await reservations.create(party_size)"
FIXED = 'await reservations.create(party_size, idempotency_key=f"table-for-{party_size}")'
SEARCHED = "earful.explore(booking).assert_contracts()"
ALLOWED = 'earful.explore(booking).assert_contracts(allow=["M2/rule d", "M1/rule 2"])'

pytestmark = pytest.mark.slow


@pytest.fixture
def example(pytester):
    for name in ("bot.py", "test_booking.py"):
        shutil.copy(EXAMPLE / name, pytester.path / name)
    return pytester


def run(example):
    return example.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")


def _change(path, old, new):
    source = path.read_text()
    assert source.count(old) == 1
    path.write_text(source.replace(old, new))


def test_pipecat_as_shipped_the_conversation_passes_and_the_search_finds_a_second_booking(example):
    result = run(example)
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*ContractFailure: booking on pipecat-ai *contract rule(s) broken*",
            "*A2/rule  in *",
            "*2 durable effects for 'book-table'; the scenario authorized 1*",
            "search 1 (booking) breaks A2/rule: backend.timeout 1*",
        ]
    )
    kept = example.path / ".earful" / "test_booking.py-test_every_timing_and_fault_near_it"
    assert (kept / "search-1.booking.A2-rule.case.json").exists()
    # The page quotes the failure line for line.
    (quoted,) = [
        block
        for block in re.findall(r"```text\n(.*?)```", PAGE, flags=re.DOTALL)
        if "ContractFailure" in block
    ]
    said = result.stdout.str()
    for line in filter(None, (line.strip() for line in quoted.splitlines())):
        assert line in said, line


def test_pipecat_with_a_key_sent_the_second_booking_is_gone_and_the_page_ends_green(example):
    """The walkthrough's two steps: send the key, then allow what a hangup leaves behind."""
    _change(example.path / "bot.py", AS_SHIPPED, FIXED)
    result = run(example)
    result.assert_outcomes(passed=1, failed=1)
    said = result.stdout.str()
    assert "A2/rule" not in said
    assert set(re.findall(r"^E\s+([ALM]\d/rule(?: \w)?)  in ", said, re.MULTILINE)) == {
        "M2/rule d",
        "M1/rule 2",
    }
    _change(example.path / "test_booking.py", SEARCHED, ALLOWED)
    run(example).assert_outcomes(passed=2)


def test_pipecat_the_page_shows_the_example_and_its_two_changes_as_they_are():
    shown = re.findall(r"```python\n(.*?)```", PAGE, flags=re.DOTALL)
    for name in ("bot.py", "test_booking.py"):
        assert (EXAMPLE / name).read_text() in shown, name
    assert f"confirmation = {FIXED}" in PAGE and ALLOWED in PAGE
    assert f"confirmation = {AS_SHIPPED}\n" in (EXAMPLE / "bot.py").read_text()
    assert SEARCHED in (EXAMPLE / "test_booking.py").read_text()


def _registers_its_functions(llm, reservations):
    """The application's set-up the skill's example imports from mybot.bot."""

    async def create_reservation(params):
        receipt = await reservations.create(params.arguments["party_size"])
        await params.result_callback(f"Booked: {receipt}.")

    llm.register_function("create_reservation", create_reservation)


@pytest.mark.xfail(
    strict=True,
    raises=KnownDefect,
    reason="Earful: the skill's Pipecat example fails its own search",
)
def test_the_skills_pipecat_example_passes_its_own_two_tests(monkeypatch):
    from earful.pipecat import ContractFailure, explore, run

    skill = (ROOT / "skills" / "earful" / "SKILL.md").read_text()
    section = skill.split("## A Pipecat bot", 1)[1]
    (block,) = re.findall(r"```python\n(.*?)```", section, flags=re.DOTALL)[:1]
    bot = types.ModuleType("mybot.bot")
    bot.register_functions = _registers_its_functions
    monkeypatch.setitem(sys.modules, "mybot", types.ModuleType("mybot"))
    monkeypatch.setitem(sys.modules, "mybot.bot", bot)
    names: dict = {}
    exec(compile(block, "SKILL.md", "exec"), names)  # noqa: S102
    run(names["booking"]).assert_contracts()  # its first test
    try:
        explore(names["booking"], workers=0).assert_contracts()  # its second
    except ContractFailure as exc:
        raise KnownDefect(str(exc).splitlines()[0]) from exc
