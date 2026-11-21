"""The committed reference ledgers: this machine reproduces them."""

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk

from earful.core.ledger import first_difference, normalize
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case


# tests/golden changes only with scripts/check_all.sh --update, and the diff is reviewed.
@pytest.mark.parametrize("name", sorted(CASES))
def test_run_reproduces_the_committed_ledger(name, reference_ledger):
    path = reference_ledger(name)
    lines = normalize(run_case(CASES[name]()).ledger.events)
    assert first_difference(path.read_text("ascii").splitlines(), lines) is None


def test_only_a_release_with_no_reference_ledgers_at_all_skips_the_comparison(
    reference_ledger, monkeypatch
):
    monkeypatch.setattr(lk, "__version__", "1.8.999")
    with pytest.raises(pytest.skip.Exception, match=r"livekit-agents 1\.8\.999 has no reference"):
        reference_ledger("barge_in_mid_word")
    monkeypatch.setattr(lk, "__version__", "1.8.12")
    assert reference_ledger("barge_in_mid_word").name.startswith("lk-1.8.12.barge_in_mid_word.")
    with pytest.raises(BaseException) as missing:  # a skip here would pass unnoticed
        reference_ledger("no_such_case")
    assert isinstance(missing.value, AssertionError)
    assert "no reference ledger of no_such_case" in str(missing.value)
