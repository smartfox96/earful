"""Record session events and collect the warnings issued during a run."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import livekit.agents as lk

from ..core.backend import BackendError, BackendTimeout
from ..core.ledger import Ledger
from . import options, seams
from .providers import (
    summarize_item,
)

if TYPE_CHECKING:
    from livekit.agents.voice.events import EventTypes


def _check_handover(
    session: lk.AgentSession[Any], checked: list[lk.Agent], errors: list[str]
) -> None:
    """An agent that becomes active later, by a handover or a task, is held to the same."""
    try:
        agent = session.current_agent
    except RuntimeError:  # between two activities, or closed
        return
    if any(agent is seen for seen in checked):
        return
    checked.append(agent)
    refusal = options.agent_refusal(agent, session)
    if refusal is not None:
        errors.append(f"an agent that became active during the run: {refusal}")


def _tap(
    session: lk.AgentSession[Any], ledger: Ledger, errors: list[str], *, checked: list[lk.Agent]
) -> None:
    # A handler must never raise into the framework: failures are collected and fail the run.
    def on(name: EventTypes, fields: Callable[[Any], dict[str, Any]]) -> None:
        def handler(ev: Any) -> None:
            try:
                _check_handover(session, checked, errors)
                ledger.append("framework", f"fw.{name}", **fields(ev))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"tap {name}: {type(exc).__name__}: {exc}")

        session.on(name, handler)

    on("agent_state_changed", lambda e: {"old": e.old_state, "new": e.new_state})
    on("user_state_changed", lambda e: {"old": e.old_state, "new": e.new_state})
    on("user_input_transcribed", lambda e: {"text": e.transcript, "final": e.is_final})
    on("conversation_item_added", lambda e: {"item": summarize_item(e.item)})
    on(
        "function_tools_executed",
        lambda e: {
            "calls": [summarize_item(c) for c in e.function_calls],
            "outputs": [summarize_item(o) for o in e.function_call_outputs if o is not None],
        },
    )
    on(
        "speech_created",
        lambda e: {
            "speech_id": e.speech_handle.id,
            "source": e.source,
            "user_initiated": e.user_initiated,
        },
    )
    on("agent_false_interruption", lambda e: {})
    on("close", lambda e: {"reason": seams.close_reason(e.reason)})
    on("error", lambda e: {"error": type(e.error).__name__})


class _WarningCollector(logging.Handler):
    """Counts livekit-agents' warnings whatever level its loggers are set to, and passes on
    the records the application's handlers would have had."""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__()
        self.messages: list[str] = []
        self._logger = logger
        self._handlers, self._propagate = list(logger.handlers), logger.propagate
        # Each level that hides a warning, restored by ``close``.
        self._levels = {
            one: one.level
            for one in (logger, *_descendants(logger))
            if one.level > logging.WARNING
            or (one is logger and logger.getEffectiveLevel() > logging.WARNING)
        }

    def open(self) -> None:
        for one in self._levels:
            one.setLevel(logging.WARNING)
        self._logger.handlers, self._logger.propagate = [self], False

    def close(self) -> None:
        self._logger.handlers, self._logger.propagate = self._handlers, self._propagate
        for one, level in self._levels.items():
            one.setLevel(level)
        super().close()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.messages.append(f"{record.name}: {record.getMessage()}")
        if record.levelno < self._shown(record.name):
            return
        raised = record.exc_info[1] if record.exc_info else None
        if isinstance(raised, (BackendError, BackendTimeout)):  # a fault the scenario declared
            record = logging.makeLogRecord(
                {**record.__dict__, "msg": f"{record.getMessage()}: {raised}", "args": None}
            )
            record.exc_info = record.exc_text = None
        # As propagation would, with this handler found, so Python's last resort is not used.
        handlers, above = self._handlers, self._logger.parent if self._propagate else None
        while True:
            for handler in handlers:
                if record.levelno >= handler.level:
                    handler.handle(record)
            if above is None:
                return
            handlers, above = above.handlers, above.parent if above.propagate else None

    def _shown(self, name: str) -> int:
        """The least level the logger ``name`` let through before the run."""
        one: logging.Logger | None = logging.getLogger(name)
        while one is not None:
            level = self._levels.get(one, one.level)
            if level:
                return level
            one = one.parent
        return logging.WARNING


def _descendants(logger: logging.Logger) -> list[logging.Logger]:
    prefix = logger.name + "."
    found = logging.Logger.manager.loggerDict.items()
    return [
        one for name, one in found if name.startswith(prefix) and isinstance(one, logging.Logger)
    ]
