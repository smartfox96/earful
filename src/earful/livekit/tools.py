"""Tool witness: records each call of a function tool as the world saw it."""

from __future__ import annotations

import asyncio
import functools
import inspect
import typing
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

import livekit.agents as lk
from livekit.agents import llm as lk_llm

from ..core.backend import Arguments, Effect, current_tool
from ..core.ledger import Ledger, json_value

_CONTEXT_PARAM = "earful_run_context"
_MARK = "__earful_witnessed__"
_EFFECT = "__earful_effect__"


def _plain(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, dict):
        return all(isinstance(k, str) and _plain(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_plain(v) for v in value)
    return False


def model_text(value: Any) -> str | None:
    """The text livekit-agents shows the model for a tool's return value, or None for a
    value whose ``str`` is not a stable function of its content."""
    if value is None:
        return ""
    if not _plain(value):
        return None
    return str(value)


def expects_reply(outcome: Any) -> bool:
    """Whether livekit-agents gives the model another call after a tool ends this way.
    A tool that returns nothing, or raises ``StopResponse``, asks for silence."""
    if isinstance(outcome, lk.StopResponse) or outcome is None:
        return False
    required = getattr(outcome, "reply_required", None)
    return True if required is None else bool(required)


class ToolWitness:
    """Per-run record of tool invocations. ``effects`` maps a tool name to its declared write."""

    def __init__(self, ledger: Ledger, effects: Mapping[str, Effect] | None = None) -> None:
        self._ledger = ledger
        self.effects = dict(effects or {})
        self._invocations = 0
        self._running: dict[int, asyncio.Task[Any] | None] = {}

    async def cancel_running(self) -> None:
        """Cancel every tool still running, in the order they were called."""
        tasks = [task for _tool, task in sorted(self._running.items()) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def called(self, name: str, call_id: str | None, arguments: dict[str, Any]) -> int:
        self._invocations += 1
        self._running[self._invocations] = asyncio.current_task()
        self._ledger.append(
            "world",
            "tool.called",
            tool=self._invocations,
            name=name,
            call_id=call_id,
            arguments=json_value(arguments),
        )
        return self._invocations

    def returned(self, tool: int, name: str, output: Any) -> None:
        self._running.pop(tool, None)
        self._ledger.append(
            "world",
            "tool.returned",
            tool=tool,
            name=name,
            output=json_value(output),
            text=model_text(output),
            expects_reply=expects_reply(output),
        )

    def raised(self, tool: int, name: str, exc: BaseException) -> None:
        self._running.pop(tool, None)
        self._ledger.append(
            "world",
            "tool.raised",
            tool=tool,
            name=name,
            error=type(exc).__name__,
            message=str(exc),
            expects_reply=expects_reply(exc),
        )

    def cancelled(self, tool: int, name: str) -> None:
        self._running.pop(tool, None)
        self._ledger.append("world", "tool.cancelled", tool=tool, name=name)


_witness: ContextVar[ToolWitness | None] = ContextVar("earful_tool_witness", default=None)


def install(witness: ToolWitness | None) -> Any:
    """Make ``witness`` the recorder for tool calls in this context; returns a reset token."""
    return _witness.set(witness)


def uninstall(token: Any) -> None:
    _witness.reset(token)


def _is_run_context(hint: Any) -> bool:
    origin = typing.get_origin(hint) or hint
    return isinstance(origin, type) and issubclass(origin, lk.RunContext)


def _hints(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        return typing.get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001 - unresolvable forward references
        return dict(getattr(fn, "__annotations__", {}))


def witnessed(
    fn: Callable[..., Any], *, name: str | None = None, effect: Effect | None = None
) -> Callable[..., Any]:
    """Wrap an async tool so that each call in a run is recorded. Wrapping twice is a no-op.
    With ``effect``, a call in a run makes that write in place of calling ``fn``.
    Raises ``TypeError`` for a function that is not async."""
    if getattr(fn, _MARK, False):
        return fn
    if inspect.isfunction(fn) and not inspect.iscoroutinefunction(fn):
        raise TypeError("witnessed tools must be async functions")

    # Written with ``if``: mypy reads ``name or getattr(...)`` as a name that may be None.
    tool_name = name if name else getattr(fn, "__name__", type(fn).__name__)
    signature = inspect.signature(fn)
    hints = _hints(fn)
    declared = next((n for n in signature.parameters if _is_run_context(hints.get(n))), None)
    receiver = next(iter(signature.parameters), None)
    receiver = receiver if receiver in ("self", "cls") else None

    # A framework tool object keeps its own state in ``__dict__``, which must not be copied.
    updated = functools.WRAPPER_UPDATES if inspect.isfunction(fn) else ()

    @functools.wraps(fn, updated=updated)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        injected = kwargs.pop(_CONTEXT_PARAM, None) if declared is None else None
        witness = _witness.get()
        if witness is None:
            return await fn(*args, **kwargs)

        arguments: Arguments | None
        try:
            # However the framework passed them, by position or by name.
            arguments = dict(signature.bind_partial(*args, **kwargs).arguments)
        except TypeError:
            arguments = None  # the call itself will raise; record it with what is known
        bound = dict(arguments or {})
        context = bound.pop(declared, None) if declared is not None else injected
        if receiver is not None:
            bound.pop(receiver, None)
        call = getattr(context, "function_call", None)
        # The name the model called the tool by, which may differ from the function's.
        called_as = getattr(call, "name", None) or tool_name
        tool = witness.called(called_as, getattr(call, "call_id", None), bound)
        write = witness.effects.get(called_as, effect)
        token = current_tool.set(tool)
        try:
            if write is None or arguments is None:
                result = await fn(*args, **kwargs)
            else:
                result = await write.make(bound)
        except asyncio.CancelledError:
            witness.cancelled(tool, called_as)
            raise
        except BaseException as exc:
            witness.raised(tool, called_as, exc)
            raise
        finally:
            current_tool.reset(token)
        witness.returned(tool, called_as, result)
        return result

    # The call id comes from RunContext: a tool that declares none is shown with one.
    if declared is None:
        params = list(signature.parameters.values())
        extra = inspect.Parameter(
            _CONTEXT_PARAM, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=lk.RunContext
        )
        tail = [p for p in params if p.kind == inspect.Parameter.VAR_KEYWORD]
        head = [p for p in params if p.kind != inspect.Parameter.VAR_KEYWORD]
        shown = signature.replace(parameters=[*head, extra, *tail])
        annotations = {**hints, _CONTEXT_PARAM: lk.RunContext}
    else:
        shown = signature
        annotations = dict(hints)
    wrapper.__signature__ = shown  # type: ignore[attr-defined]
    # In this order: on Python 3.14 (PEP 649) assigning ``__annotations__`` clears ``__annotate__``.
    wrapper.__annotations__ = annotations
    wrapper.__annotate__ = lambda _format=1: dict(annotations)  # type: ignore[attr-defined]
    setattr(wrapper, _MARK, True)
    setattr(wrapper, _EFFECT, effect)
    return wrapper


def effect_tool(
    intent: str,
    *,
    payload: Callable[[Arguments], dict[str, Any]] | None = None,
    key: str | Callable[[Arguments], str | None] | None = None,
    result: Callable[[str, Arguments], Any] | None = None,
    latency: float = 0.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare that a call of the decorated tool is one durable write for ``intent``.
    The arguments are those of ``earful.core.backend.Effect``. In a run the write is made
    in place of the tool's body. Raises ``TypeError`` on a tool that is already witnessed."""
    declared = Effect(intent, payload=payload, key=key, result=result, latency=latency)

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if getattr(fn, _MARK, False):
            raise TypeError(
                f"{getattr(fn, '__name__', fn)!r} is already witnessed: "
                "effect_tool witnesses the tool itself, so it replaces @witnessed"
            )
        return witnessed(fn, effect=declared)

    return decorate


def declared_effect(tool: Any) -> Effect | None:
    """The write ``effect_tool`` declared for a framework function tool, if it did."""
    found = getattr(tool, _EFFECT, None)
    return found if isinstance(found, Effect) else None


def witness_tool(tool: Any) -> Any:
    """A recording copy of a framework function tool; any other tool is returned as is."""
    if not isinstance(tool, (lk_llm.FunctionTool, lk_llm.RawFunctionTool)):
        return tool
    if getattr(tool, _MARK, False):
        return tool
    # The constructor ``function_tool`` uses, so the copy keeps the tool's info on every release.
    return type(tool)(witnessed(tool, name=tool.info.name), tool.info)  # type: ignore[arg-type]


def _tool_effects(tools: list[Any]) -> dict[str, Effect | None]:
    return {
        t.info.name: declared_effect(t)
        for t in tools
        if isinstance(t, (lk_llm.FunctionTool, lk_llm.RawFunctionTool))
    }


async def witness_agent_tools(agent: lk.Agent) -> dict[str, Effect | None]:
    """Replace each of ``agent``'s function tools with a recording copy. Returns each
    tool's name with the write ``effect_tool`` declared for it, if any."""
    originals = list(agent.tools)
    await agent.update_tools([witness_tool(t) for t in originals])
    return _tool_effects(originals)


def witness_session_tools(session: lk.AgentSession[Any]) -> dict[str, Effect | None]:
    """The same for the tools the session gives every agent, and for each agent the
    session is handed over to from here on."""
    originals = list(session.tools)
    session.tools[:] = [witness_tool(t) for t in originals]
    update_agent = session.update_agent

    def witnessed_update(agent: lk.Agent) -> None:
        # An agent that is not running takes its tools without waiting.
        setting = agent.update_tools([witness_tool(t) for t in agent.tools])
        try:
            setting.send(None)
        except StopIteration:
            pass
        else:
            setting.close()  # already running: the model's next call names what is missed
        update_agent(agent)

    setattr(session, "update_agent", witnessed_update)  # noqa: B010 - an instance's own copy
    return _tool_effects(originals)


def unwitnessed(tools: list[Any]) -> list[str]:
    """The function tools among ``tools`` whose calls would not be recorded."""
    return sorted(
        t.info.name
        for t in tools
        if isinstance(t, (lk_llm.FunctionTool, lk_llm.RawFunctionTool))
        and not getattr(t, _MARK, False)
    )


__all__ = [
    "ToolWitness",
    "declared_effect",
    "effect_tool",
    "install",
    "json_value",
    "model_text",
    "uninstall",
    "unwitnessed",
    "witness_agent_tools",
    "witness_session_tools",
    "witness_tool",
    "witnessed",
]
