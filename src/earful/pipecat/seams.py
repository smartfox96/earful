"""Access to framework internals used by the Pipecat adapter."""

from __future__ import annotations

from typing import Any


def _setting(of: object, name: str) -> Any:
    try:
        return getattr(of, name)
    except AttributeError:
        raise ValueError(
            f"this pipecat-ai release keeps {type(of).__name__}.{name} where Earful cannot "
            "read it, so the run's header could not state it"
        ) from None


def aggregator_params(obj: object) -> Any:
    return _setting(obj, "_params")


def turn_controller(obj: object) -> Any:
    return _setting(obj, "_user_turn_controller")


def interruptions_enabled(obj: object) -> Any:
    return _setting(obj, "_enable_interruptions")


def uses_interim(obj: object) -> Any:
    return _setting(obj, "_use_interim")


def speech_timeout(obj: object) -> Any:
    return _setting(obj, "_user_speech_timeout")


def schema_handlers(tools: Any) -> list[Any]:
    return [tool for tool in getattr(tools, "standard_tools", ()) if getattr(tool, "handler", None)]


def external_summary_model(params: object, override: object | None) -> bool:
    config = getattr(params, "auto_context_summarization_config", None)
    summary = override or getattr(config, "summary_config", None)
    return getattr(summary, "llm", None) is not None


def summaries_enabled(params: object) -> bool:
    return bool(getattr(params, "enable_auto_context_summarization", False))


PRIVATE = ("_functions", "_sync_registered_tool_handlers")


def service_member(service: Any, name: str) -> Any:
    try:
        return getattr(service, name)
    except AttributeError:
        raise ValueError(
            f"this pipecat-ai release keeps LLMService.{name} where Earful cannot read it, "
            "so the functions the bot registers could not be witnessed"
        ) from None


def check_service(service: Any) -> None:
    cancel_tools(service)
    for name in PRIVATE:
        service_member(service, name)


def registered(service: Any) -> Any:
    return service_member(service, "_functions")


def cancel_tools(service: Any) -> Any:
    if hasattr(service, "_extension_tool_names"):  # 1.17 moved cancellation into extensions
        return service_member(service, "_extension_tool_names")
    return service_member(service, "_cancel_tool_names")


def sync_handlers(service: Any, advertised: Any) -> None:
    service_member(service, "_sync_registered_tool_handlers")(advertised)


def advertised_tools(context: Any) -> Any:
    return getattr(context, "tools", None)


def declared_functions(reader: Any) -> list[str]:
    cancel: set[str] = cancel_tools(reader)
    return [
        name
        for name in getattr(reader, "_functions", {})
        if name is not None and name not in cancel
    ]
