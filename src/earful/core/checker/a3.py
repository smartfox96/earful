"""A3. Truthful confirmation: the user does not hear that an action succeeded before the
backend has recorded it."""

from __future__ import annotations

from . import policy as policies
from .report import ContractResult, Finding, result, verdict
from .view import View


def check(view: View, policy: policies.A3) -> ContractResult:
    verdicts, notes = [], []
    for reply in view.replies:
        assert reply.response is not None
        playout = view.playout(reply.call)
        for claim in reply.response["data"]["claims"]:
            intent = claim["intent"]
            instance = {
                "reply": reply.call,
                "intent": intent,
                "words": [claim["first_word"], claim["last_word"]],
            }
            base = [reply.response, playout.utterance.event if playout.utterance else None]
            missing = playout.unattributable or (
                "the run ended before the reply's playout finished" if playout.pending else None
            )
            if missing:
                verdicts.append(
                    verdict(
                        "A3",
                        instance,
                        f"cannot tell whether the claim about {intent!r} was heard",
                        events=base,
                        unobservable=missing,
                    )
                )
                continue
            if claim["last_word"] not in playout.completed_us:
                notes.append(
                    f"reply {reply.call}: the claim about {intent!r} was not delivered "
                    "(its last word was cut or never played)"
                )
                continue

            if policy.claim_point == "span_end":
                heard_at = playout.completed_us[claim["last_word"]]
            else:
                heard_at = view.word_start_us(playout, claim["first_word"])
            finishes = [s.finished for s in playout.segments]
            if heard_at is None:
                verdicts.append(
                    verdict(
                        "A3",
                        instance,
                        f"cannot time the start of the claim about {intent!r}",
                        events=[*base, *finishes],
                        unobservable="the start of the claim's first word cannot be derived",
                    )
                )
                continue

            commits = sorted(
                (e.committed for e in view.effects.values() if e.intent == intent and e.committed),
                key=lambda c: c["seq"],
            )
            in_time = [c for c in commits if c["t_us"] <= heard_at]
            findings = []
            if not in_time:
                later = commits[0]["t_us"] - heard_at if commits else None
                detail = (
                    f"the claim was heard at {heard_at} us; the first effect for {intent!r} "
                    + (
                        f"committed {later} us later"
                        if later is not None
                        else "was never committed"
                    )
                )
                findings.append(Finding("rule", detail, tuple(c["seq"] for c in commits[:1])))
            verdicts.append(
                verdict(
                    "A3",
                    instance,
                    f"the claim about {intent!r} was heard after the effect was committed"
                    if not findings
                    else f"the user heard that {intent!r} succeeded before the backend recorded it",
                    events=[*base, *finishes, *(in_time[:1] or commits[:1])],
                    findings=findings,
                    flags=["most_favourable_reading"] if reply.call in view.read_calls else [],
                    measures={
                        "claim_heard_us": heard_at,
                        "first_commit_us": commits[0]["t_us"] if commits else None,
                        "commit_before_claim_us": heard_at - in_time[0]["t_us"]
                        if in_time
                        else None,
                    },
                )
            )
    return result("A3", policy, verdicts, notes)
