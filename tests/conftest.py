"""Shared test setup: ``pytester``, and the reference ledgers of the installed release."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def reference_ledger():
    """Gives a case's committed ledger for the installed livekit-agents, or skips the test."""
    import livekit.agents as lk  # absent where only Pipecat is installed

    def committed(case):
        release = lk.__version__
        # A release the extra admits and docs/compatibility.md does not list has none.
        if not any(GOLDEN.glob(f"lk-{release}.*")):
            pytest.skip(f"livekit-agents {release} has no reference ledgers")
        path = GOLDEN / f"lk-{release}.{case}.ledger.normalized.jsonl"
        assert path.exists(), f"no reference ledger of {case} for livekit-agents {release}"
        return path

    return committed
