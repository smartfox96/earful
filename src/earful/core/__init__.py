"""The portable core: ledger, validator, contract checker, effect backend, case format,
boundary scheduler, shrinker and failure bundle.

Nothing here imports a voice framework. An adapter supplies one function, from a case to
the ledger of running it, and everything else works on plain data.

The names most code needs are here. Each module holds the rest:

    backend     the effect backend, ``effect`` and ``Effect``
    bundle      a kept failure: ``capture``, ``load``, ``replay``
    case        a case as data (earful.case/1) and its variables
    checker     ``check`` and the report it returns
    ledger      the event ledger, its normal form and its hash
    predicate   a contract rule that a run breaks
    schema      ``validate`` and ``validity`` for earful.ledger/1
    search      the boundary scheduler: ``explore``
    shrink      reduction of a failing case
    timeline    a ledger as text, for a person
"""

from .backend import BackendError, BackendTimeout, Effect, EffectBackend, effect
from .checker import ContractFailure, ContractResult, Finding, Report, Verdict, check
from .ledger import Ledger, ledger_hash, normalize, read_jsonl, write_jsonl
from .predicate import Predicate
from .schema import validate, validity

__all__ = [
    "BackendError",
    "BackendTimeout",
    "ContractFailure",
    "ContractResult",
    "Effect",
    "EffectBackend",
    "Finding",
    "Ledger",
    "Predicate",
    "Report",
    "Verdict",
    "check",
    "effect",
    "ledger_hash",
    "normalize",
    "read_jsonl",
    "validate",
    "validity",
    "write_jsonl",
]
