"""The seven contracts, each declared once: its title, policy, rules and missing-verdict codes."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    tool_wait_us: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class L2:
    stop_bound_us: int = 100_000
    start_grace_us: int = 0
    interruptions_allowed: bool = True


@dataclass(frozen=True)
class Contract:
    name: str
    title: str
    statement: str
    policy: type  # its policy values and their defaults
    rules: tuple[str, ...]  # the rules a failed verdict can name (docs/contracts.md, section 6)
    codes: tuple[str, ...]  # why a verdict can be missing (docs/contracts.md, section 5)
    exercised_by: str  # what a run holds when the contract has an instance to judge


# Each contract's check is ``check`` in the checker module named after it, such as ``a1``.
DECLARED: dict[str, Contract] = {
    contract.name: contract
    for contract in (
        Contract(
            name="A1",
            title="Retired intent",
            statement="After the user corrects or cancels an intent, no new write for it starts.",
            policy=A1,
            rules=("rule", "in-flight"),
            codes=("outside-backend",),
            exercised_by="speech that retires an intent and whose transcript was delivered",
        ),
        Contract(
            name="A2",
            title="One logical effect",
            statement="Durable effects for an intent do not exceed the count the scenario "
            "authorized.",
            policy=A2,
            rules=("rule",),
            codes=("outside-backend",),
            exercised_by="write request for a declared intent",
        ),
        Contract(
            name="A3",
            title="Truthful confirmation",
            statement="A claim of success finishes playing only after the backend has recorded "
            "the effect.",
            policy=A3,
            rules=("rule",),
            codes=(
                "run-ended",
                "outside-backend",
                "audio-unattributed",
                "readings-over-cap",
                "word-start-unknown",
            ),
            exercised_by="claim whose last word was played",
        ),
        Contract(
            name="M1",
            title="Played vs. remembered",
            statement="What the agent remembers having said matches what was played.",
            policy=M1,
            rules=("rule 2", "rule 3", "rule 4", "rule 5"),
            codes=(
                "run-ended",
                "no-memory-after",
                "audio-unattributed",
                "readings-over-cap",
                "memory-unaligned",
                "handoff-context",
                "history-before-playout",
            ),
            exercised_by="reply or spoken text whose playout finished or was abandoned",
        ),
        Contract(
            name="M2",
            title="Turn integrity",
            statement="Each model call is shown the conversation that happened, once each, in "
            "order.",
            policy=M2,
            rules=("rule a", "rule b", "rule c", "rule d", "rule e", "rule f"),
            codes=("no-stable-text", "tool-unwitnessed", "handoff-context"),
            exercised_by="model call",
        ),
        Contract(
            name="L1",
            title="Progress",
            statement="A turn that is owed a response gets one within its budget.",
            policy=L1,
            rules=("milestone 1", "milestone 2", "milestone 3"),
            codes=(
                "run-ended",
                "audio-unattributed",
                "readings-over-cap",
                "handoff-context",
                "tool-unwitnessed",
            ),
            exercised_by="delivered speech that expects a reply",
        ),
        Contract(
            name="L2",
            title="Interruption",
            statement="While the user holds the floor the agent is silent.",
            policy=L2,
            rules=("rule a", "rule b"),
            codes=("no-origin", "no-interruption-point", "no-reply-under-way"),
            exercised_by="speech the VAD reported that is neither noise nor a backchannel",
        ),
    )
}
