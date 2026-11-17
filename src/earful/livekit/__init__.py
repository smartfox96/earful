"""The LiveKit Agents adapter: what a test of a LiveKit agent imports.

    from earful.livekit import Case, ScriptedModel, UserSpeech, explore, run, user_said

Needs livekit-agents, which the ``livekit`` extra installs:

    pip install "earful[livekit]"
"""

try:
    import livekit.agents as _lk  # noqa: F401
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] != "livekit":
        raise  # livekit-agents is there, and something it needs is not
    raise ImportError(
        "earful.livekit needs livekit-agents, which is not installed. "
        'Install it with: pip install "earful[livekit]"'
    ) from exc

from ..core.backend import BackendError, BackendTimeout, Effect, effect
from ..core.checker import ContractFailure
from ..core.predicate import Predicate
from .api import WorkersUnavailable, cross_check, explore, keep, replay, run
from .harness import Case, Delay, Fault, Intent, InvalidRun, RunResult, run_case
from .playbook import (
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
from .providers import Claim, Policy, Reply, ToolCall, UserSpeech
from .scenario import Scenario
from .sink import ClockedSink
from .tools import effect_tool, witnessed

__all__ = [
    "BackendError",
    "BackendTimeout",
    "Case",
    "Claim",
    "ClockedSink",
    "Condition",
    "Context",
    "ContractFailure",
    "Delay",
    "Effect",
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
    "effect_tool",
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
    "witnessed",
]
