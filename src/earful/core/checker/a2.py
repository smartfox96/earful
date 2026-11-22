"""A2. One logical effect: durable effects for an intent do not exceed the authorized count."""

from __future__ import annotations

from . import policy as policies
from .report import ContractResult, Finding, result, verdict
from .view import View


def check(view: View, policy: policies.A2) -> ContractResult:
    verdicts = []
    for intent, declared in view.intents.items():
        instance = {"intent": intent}
        effects = [e for e in view.effects.values() if e.intent == intent]
        if not effects:
            calls = view.unaccounted_calls(intent)
            if calls:
                verdicts.append(
                    verdict(
                        "A2",
                        instance,
                        f"cannot count effects for {intent!r}",
                        events=calls,
                        missing=[
                            (
                                "outside-backend",
                                "a tool for the intent was called but nothing reached the backend",
                            )
                        ],
                    )
                )
            continue  # no tool call and no write: the intent was never acted on

        commits = [e.committed for e in effects if e.committed is not None]
        became = [e.outcome["kind"] if e.outcome else None for e in effects]
        authorized = declared["authorized"]
        findings = []
        if len(commits) > authorized:
            findings.append(
                Finding(
                    "rule",
                    f"{len(commits)} durable effects for {intent!r}; "
                    f"the scenario authorized {authorized}",
                    tuple(c["seq"] for c in commits),
                )
            )
        verdicts.append(
            verdict(
                "A2",
                instance,
                f"{len(commits)} durable effect(s) for {intent!r}, {authorized} authorized",
                events=[*(e.requested for e in effects), *(e.outcome for e in effects)],
                findings=findings,
                measures={
                    "committed": len(commits),
                    "authorized": authorized,
                    "requested": len(effects),
                    "deduplicated": became.count("effect.deduplicated"),
                    "failed": became.count("effect.failed"),
                    "no_outcome": became.count(None),
                    "caller_timed_out": sum(1 for e in effects if e.timed_out),
                    "backend_mode": view.providers["backend"]["mode"],
                },
            )
        )
    return result("A2", policy, verdicts)
