"""The Pipecat adapter. Needs pipecat-ai: pip install "earful[pipecat]"."""

from ..scripted.releases import importing as _importing

try:
    import pipecat as _pc
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] != "pipecat":
        raise  # pipecat-ai is installed; one of its own imports failed
    raise ImportError(
        "earful.pipecat needs pipecat-ai, which is not installed. "
        'Install it with: pip install "earful[pipecat]"'
    ) from exc

with _importing("pipecat-ai", _pc.__version__):
    from ..core.backend import BackendError, BackendTimeout, Effect, effect
    from ..core.checker import ContractFailure
    from ..core.predicate import Predicate
    from ..scripted.case import Delay, Fault, Intent, InvalidRun
    from ..scripted.model import Claim, Policy, Reply, ToolCall
    from ..scripted.playbook import (
        Condition,
        Context,
        ScriptedModel,
        ToolResult,
        reply,
        tool_failed,
        tool_returned,
        tool_succeeded,
        user_ever_said,
        user_said,
    )
    from ..scripted.world import UserSpeech
    from .api import WorkersUnavailable, cross_check, explore, keep, replay, run
    from .harness import Case, RunResult, run_case
    from .pipeline import Ends
    from .scenario import Scenario

__all__ = [
    "BackendError",
    "BackendTimeout",
    "Case",
    "Claim",
    "Condition",
    "Context",
    "ContractFailure",
    "Delay",
    "Effect",
    "Ends",
    "Fault",
    "Intent",
    "InvalidRun",
    "Policy",
    "Predicate",
    "Reply",
    "RunResult",
    "Scenario",
    "ScriptedModel",
    "ToolCall",
    "ToolResult",
    "UserSpeech",
    "WorkersUnavailable",
    "cross_check",
    "effect",
    "explore",
    "keep",
    "replay",
    "reply",
    "run",
    "run_case",
    "tool_failed",
    "tool_returned",
    "tool_succeeded",
    "user_ever_said",
    "user_said",
]
