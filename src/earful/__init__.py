"""Earful: deterministic regression tests for real-time voice agents.

This package is the framework-independent part: the ledger, the contract checker, the
backend a test's side effects are recorded in, and the names a stand-in service or a
ledger file needs. It imports no voice framework.

    from earful import check, read_ledger

    report = check(read_ledger("run.ledger.jsonl"))
    report.assert_contracts()

Running an agent is the adapter's part. For LiveKit Agents, installed with
``pip install "earful[livekit]"``:

    from earful.livekit import Case, ScriptedModel, UserSpeech, explore, run
"""

from importlib import metadata as _metadata

from .core.backend import BackendError, BackendTimeout, Effect, effect
from .core.checker import ContractFailure, Report, Verdict, check
from .core.ledger import ledger_hash
from .core.ledger import read_jsonl as read_ledger
from .core.ledger import write_jsonl as write_ledger
from .core.predicate import Predicate

try:
    __version__ = _metadata.version("earful")
except _metadata.PackageNotFoundError:  # run from a source tree that is not installed
    __version__ = "0+unknown"

__all__ = [
    "BackendError",
    "BackendTimeout",
    "ContractFailure",
    "Effect",
    "Predicate",
    "Report",
    "Verdict",
    "__version__",
    "check",
    "effect",
    "ledger_hash",
    "read_ledger",
    "write_ledger",
]
