"""Earful: deterministic regression tests for real-time voice agents.
Framework adapters live in ``earful.livekit`` and ``earful.pipecat``."""

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
