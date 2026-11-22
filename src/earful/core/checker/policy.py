"""The policy values each contract accepts, with their defaults."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from .contracts import A1, A2, A3, DECLARED, L1, L2, M1, M2


class PolicyError(ValueError):
    """A scenario declared a policy value the contract does not define, or of the wrong kind."""


POLICIES: dict[str, type] = {name: contract.policy for name, contract in DECLARED.items()}


_CHOICES = {
    ("A1", "in_flight"): ("may_finish", "must_not_commit"),
    ("A3", "claim_point"): ("span_end", "span_start"),
}


def parse(contract: str, declared: dict[str, Any] | None) -> Any:
    """The policy for ``contract``: its defaults, overridden by what the scenario declared."""
    cls = POLICIES[contract]
    declared = dict(declared or {})
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(declared.keys() - known.keys())
    if unknown:
        raise PolicyError(
            f"{contract}: unknown policy values {unknown}; it defines {sorted(known)}"
        )
    values: dict[str, Any] = {}
    for name, value in declared.items():
        default = known[name].default
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        elif isinstance(default, tuple):
            ok = isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
            value = tuple(value) if ok else value
        else:
            ok = isinstance(value, str)
        choices = _CHOICES.get((contract, name))
        if ok and choices and value not in choices:
            raise PolicyError(f"{contract}: {name} must be one of {list(choices)}, not {value!r}")
        if not ok:
            raise PolicyError(f"{contract}: {name} has the wrong kind of value: {value!r}")
        values[name] = value
    return cls(**values)


def parse_all(declared: dict[str, Any] | None) -> dict[str, Any]:
    declared = declared or {}
    unknown = sorted(declared.keys() - POLICIES.keys())
    if unknown:
        raise PolicyError(f"unknown contracts {unknown}; choose from {sorted(POLICIES)}")
    return {name: parse(name, declared.get(name)) for name in POLICIES}


__all__ = [
    "A1",
    "A2",
    "A3",
    "L1",
    "L2",
    "M1",
    "M2",
    "POLICIES",
    "PolicyError",
    "parse",
    "parse_all",
]
