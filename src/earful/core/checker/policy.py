"""The policy values each contract accepts, with their defaults."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


class PolicyError(ValueError):
    """A scenario declared a policy value the contract does not define, or of the wrong kind."""


@dataclass(frozen=True)
class A1:
    in_flight: str = "may_finish"  # or "must_not_commit"
    reissue_allowed: tuple[str, ...] = ()  # retired intents a correction may start again


@dataclass(frozen=True)
class A2:
    pass  # the authorized count is declared on the intent; the backend mode on the scenario


@dataclass(frozen=True)
class A3:
    claim_point: str = "span_end"  # or "span_start"


@dataclass(frozen=True)
class M1:
    # How much audio memory may run ahead of, or fall behind, what was played.
    ahead_us: int = 1_000_000
    behind_us: int = 1_000_000
    check_interrupted_flag: bool = True


@dataclass(frozen=True)
class M2:
    user_commit_lag_us: int = 1_000_000


@dataclass(frozen=True)
class L1:
    slack_us: int = 1_000_000
    max_steps: int = 4


@dataclass(frozen=True)
class L2:
    stop_bound_us: int = 100_000
    start_grace_us: int = 0
    interruptions_allowed: bool = True


POLICIES: dict[str, type] = {"A1": A1, "A2": A2, "A3": A3, "M1": M1, "M2": M2, "L1": L1, "L2": L2}

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
