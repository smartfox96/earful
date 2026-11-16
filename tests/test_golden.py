"""The committed reference ledgers: this machine reproduces them, and they validate.

Change them only with ``scripts/check_all.sh --update``, and review the diff.
"""
from pathlib import Path

import livekit.agents as lk
import pytest

from earful.core.ledger import first_difference, normalize, read_jsonl
from earful.core.schema import validity
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize("name", sorted(CASES))
def test_run_reproduces_the_committed_ledger(name):
    path = GOLDEN / f"lk-{lk.__version__}.{name}.ledger.normalized.jsonl"
    assert path.exists(), f"no reference ledger for livekit-agents {lk.__version__}"
    lines = normalize(run_case(CASES[name]()).ledger.events)
    assert first_difference(path.read_text("ascii").splitlines(), lines) is None


def test_every_committed_ledger_is_a_valid_run():
    paths = sorted(GOLDEN.glob("*.ledger.normalized.jsonl"))
    assert paths
    for path in paths:
        assert validity(read_jsonl(path)) == [], path.name
