"""A1. Retired intent: after the user corrects or cancels an intent, no new write for it starts."""

from __future__ import annotations

from . import policy as policies
from .report import ContractResult, Finding, result, verdict
from .view import View


def check(view: View, policy: policies.A1) -> ContractResult:
    verdicts = []
    # Tool invocations that wrote something through the backend, for any intent.
    wrote = {e.requested["data"]["tool"] for e in view.effects.values()}
    for speech in view.delivered:
        for intent in speech.retires:
            point = speech.final
            assert point is not None
            instance = {"speech": speech.index, "intent": intent}
            effects = [e for e in view.effects.values() if e.intent == intent]
            tools = view.intents[intent]["tools"]
            # A call of its tool that wrote nothing at all leaves no way to tell what it
            # did. A call that wrote for another intent is accounted for.
            called_after = [
                t.called
                for t in view.tools.values()
                if t.name in tools and t.called["seq"] > point["seq"] and t.tool not in wrote
            ]
            if not effects and called_after:
                verdicts.append(
                    verdict(
                        "A1",
                        instance,
                        f"cannot tell whether {intent!r} was written again",
                        events=[point, *called_after],
                        unobservable="the intent's tool was called but does not write through the backend",
                    )
                )
                continue

            new = [e for e in effects if e.requested["seq"] > point["seq"]]
            late = [
                e
                for e in effects
                if e.requested["seq"] < point["seq"]
                and e.committed is not None
                and e.committed["seq"] > point["seq"]
            ]
            findings = []
            if new and intent not in policy.reissue_allowed:
                findings.append(
                    Finding(
                        "rule",
                        f"{len(new)} write(s) for {intent!r} were requested after the user retired it",
                        tuple(e.requested["seq"] for e in new),
                    )
                )
            if late and policy.in_flight == "must_not_commit":
                findings.append(
                    Finding(
                        "in-flight",
                        f"{len(late)} write(s) in flight at the retirement committed after it",
                        tuple(e.committed["seq"] for e in late),
                    )
                )
            verdicts.append(
                verdict(
                    "A1",
                    instance,
                    f"no write for {intent!r} started after the user retired it"
                    if not findings
                    else f"{intent!r} was written after the user retired it",
                    events=[point, *(e.requested for e in new), *(e.committed for e in late)],
                    findings=findings,
                    measures={"writes_after_retirement": len(new), "late_commits": len(late)},
                    flags=["late_commit"] if late else [],
                )
            )
    return result("A1", policy, verdicts)
