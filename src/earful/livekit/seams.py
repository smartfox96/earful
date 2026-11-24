"""Access to framework internals used by the LiveKit adapter."""

from __future__ import annotations

import typing
from collections.abc import Callable
from typing import Any

import livekit.agents as lk
from livekit.agents import stt as lk_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.voice import io
from livekit.agents.voice.transcription.synchronizer import TranscriptSynchronizer

__all__ = ["DEFAULT_API_CONNECT_OPTIONS", "APIConnectOptions", "TranscriptSynchronizer", "io"]


def speech_data(text: str) -> lk_stt.SpeechData:
    language: Any = ""
    code = getattr(lk_stt, "LanguageCode", None) or getattr(
        __import__("livekit.agents", fromlist=["LanguageCode"]), "LanguageCode", None
    )
    if code is not None:
        language = code("")
    return lk_stt.SpeechData(text=text, language=language)


def summarize_item(item: Any) -> dict[str, Any]:
    """A chat-context item reduced to the fields contracts care about."""
    out: dict[str, Any] = {"type": item.type, "id": getattr(item, "id", None)}
    if item.type == "message":
        out["role"] = item.role
        out["text"] = item.text_content
        out["interrupted"] = bool(getattr(item, "interrupted", False))
    elif item.type == "function_call":
        out["name"] = item.name
        out["arguments"] = item.arguments
        out["call_id"] = item.call_id
    elif item.type == "function_call_output":
        out["name"] = getattr(item, "name", None)
        out["output"] = item.output
        out["call_id"] = item.call_id
        out["is_error"] = bool(getattr(item, "is_error", False))
    return out


def vad_of(stream: Any) -> Any:
    return stream._vad


def recognizer_of(stream: Any) -> Any:
    return stream._stt


def model_of(stream: Any) -> Any:
    return stream._llm


def voice_of(stream: Any) -> Any:
    return stream._tts


def stream_input(stream: Any) -> Any:
    return stream._input_ch


def input_text(stream: Any) -> Any:
    return stream._input_text


def send_event(stream: Any, event: Any) -> None:
    stream._event_ch.send_nowait(event)


def mark_started(stream: Any) -> None:
    if "_mark_started" in dir(stream):
        stream._mark_started()


def close_reason(reason: Any) -> str:
    return str(getattr(reason, "value", reason))


def _private(agent: lk.Agent, name: str) -> Any:
    try:
        return getattr(agent, name)
    except AttributeError:
        raise ValueError(
            f"this livekit-agents release keeps Agent.{name} where Earful cannot read it, so "
            "the run's header could not state the agent's options"
        ) from None


def agent_option(agent: lk.Agent, option: str) -> Any:
    name = "_async_tool_options" if option == "tool_handling" else option
    return _private(agent, name)


def turn_handling(agent: lk.Agent) -> Any:
    return _private(agent, "_turn_handling")


def hints(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        return typing.get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001 - unresolvable forward references
        return dict(getattr(fn, "__annotations__", {}))


def reply_required(shown: Any) -> Any:
    return getattr(shown, "reply_required", None)


def is_witnessed(fn: Any) -> Any:
    return getattr(fn, _MARK, False)


def declared_effect(tool: Any) -> Any:
    return getattr(tool, _EFFECT, None)


def tool_name(fn: Any) -> Any:
    return getattr(fn, "__name__", type(fn).__name__)


def display_name(fn: Any) -> Any:
    return getattr(fn, "__name__", fn)


def function_call(context: Any) -> Any:
    return getattr(context, "function_call", None)


def called_name(call: Any) -> Any:
    return getattr(call, "name", None)


def call_id(call: Any) -> Any:
    return getattr(call, "call_id", None)


_MARK = "__earful_witnessed__"

_EFFECT = "__earful_effect__"


def mark_witnessed(wrapper: Callable[..., Any], effect: Any) -> None:
    setattr(wrapper, _MARK, True)
    setattr(wrapper, _EFFECT, effect)
