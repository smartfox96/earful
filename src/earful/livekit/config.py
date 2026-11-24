"""The session configuration and scenario header."""

from __future__ import annotations

import copy
from typing import Any

import livekit.agents as lk

from ..core.backend import Effect
from ..core.timebase import to_us
from ..scripted.header import declarations
from . import tools
from .case import Case


def _session_kwargs(case: Case) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "turn_handling": {
            # The default turn detector is an acoustic model: synthetic audio cannot fire it.
            "turn_detection": None,
            "endpointing": lk.EndpointingOptions(
                min_delay=case.min_endpointing_delay, max_delay=case.max_endpointing_delay
            ),
            "interruption": lk.InterruptionOptions(
                enabled=case.allow_interruptions,
                # The other mode is a model reached over the network.
                mode="vad",
                min_duration=case.min_interruption_duration,
                false_interruption_timeout=case.false_interruption_timeout,
            ),
            "preemptive_generation": {"enabled": case.preemptive_generation},
        },
        "aec_warmup_duration": None,
    }
    kwargs.update(case.session_kwargs)
    if "userdata" in kwargs:
        try:
            kwargs["userdata"] = copy.deepcopy(kwargs["userdata"])
        except Exception as exc:
            raise ValueError(f"session userdata cannot be copied for each run: {exc}") from exc
    return kwargs


def _declared_session(case: Case) -> dict[str, int | None]:
    return {
        "min_interruption_duration_us": to_us(case.min_interruption_duration),
        "false_interruption_timeout_us": to_us(case.false_interruption_timeout),
        "min_endpointing_delay_us": to_us(case.min_endpointing_delay),
        "max_endpointing_delay_us": to_us(case.max_endpointing_delay),
    }


def _switches(case: Case) -> dict[str, bool]:
    """The Case's on and off options, which the header lists among the extra ones when off."""
    return {
        "allow_interruptions": case.allow_interruptions,
        "preemptive_generation": case.preemptive_generation,
    }


def _effective_session(session: lk.AgentSession[Any]) -> dict[str, int | bool | None]:
    interruption, endpointing = session.options.interruption, session.options.endpointing

    def us(value: float | None) -> int | None:
        return None if value is None else to_us(value)

    return {
        "min_interruption_duration_us": us(interruption["min_duration"]),
        "false_interruption_timeout_us": us(interruption["false_interruption_timeout"]),
        "min_endpointing_delay_us": us(endpointing["min_delay"]),
        "max_endpointing_delay_us": us(endpointing["max_delay"]),
        "allow_interruptions": interruption["enabled"],
        "preemptive_generation": session.options.preemptive_generation["enabled"],
    }


def _effects(case: Case, tools_found: dict[str, Effect | None]) -> dict[str, Effect]:
    # A misspelt tool name would leave the tool's own body to run.
    unknown = sorted(set(case.effects) - set(tools_found))
    if unknown:
        raise ValueError(
            f"effects are declared for {unknown}, which are not function tools of the agent "
            f"the run starts with; it has {sorted(tools_found)}"
        )
    at_the_tool = {name: found for name, found in tools_found.items() if found is not None}
    return {**at_the_tool, **case.effects}


def _header(
    case: Case, tools_found: dict[str, Effect | None], history: list[dict[str, Any]]
) -> dict[str, Any]:
    effects = _effects(case, tools_found)
    header = declarations(case, tools_found, effects)
    if history:
        header["history"] = history
    return {
        **header,
        "session": {
            **_declared_session(case),
            # A turn ends once the VAD's silence and the endpointing delay have both passed.
            "turn_end_delay_us": max(
                to_us(case.vad_min_silence), to_us(case.min_endpointing_delay)
            ),
            "turn_detection": None,
            "extra_options": {
                **{name: off for name, off in _switches(case).items() if not off},
                **{
                    name: tools.json_value(case.session_kwargs[name])
                    for name in sorted(case.session_kwargs)
                },
            },
        },
        "providers": {
            "vad": {
                "min_speech_us": to_us(case.vad_min_speech),
                "min_silence_us": to_us(case.vad_min_silence),
            },
            "tts": {
                "seconds_per_letter_us": to_us(case.seconds_per_letter),
                "ttfb_us": to_us(case.tts_ttfb),
            },
            "sink": {"can_pause": case.sink_can_pause},
            "backend": {"mode": case.backend_mode},
        },
    }
