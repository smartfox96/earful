"""The LiveKit configuration Earful models, and what it refuses."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import livekit.agents as lk
from livekit.agents.utils import is_given

from . import seams
from .application import realtime_provider_refusal

# AgentSession options a case passes in session_kwargs as they are. The header lists them.
PASSED = {
    "tools": "tools every agent is given; each call is witnessed",
    "max_tool_steps": "how many rounds of tool calls one reply may take",
    "userdata": "the application's own state",
    "min_consecutive_speech_delay": "a pause between two replies, on the virtual clock",
    "user_away_timeout": "when the user's state turns away, on the virtual clock",
    "session_close_transcript_timeout": "how long a close waits for a transcript",
    "transcription_timeout": "how long the framework waits for a final transcript",
    "tts_text_transforms": "what the framework does to a reply's text before the voice",
}
# AgentSession options a Case field sets.
FROM_CASE = {
    "vad": "vad_min_speech and vad_min_silence",
    "stt": "the speeches",
    "llm": "policy",
    "tts": "seconds_per_letter and tts_ttfb",
    "turn_handling": (
        "min_endpointing_delay, max_endpointing_delay, min_interruption_duration, "
        "false_interruption_timeout, allow_interruptions and preemptive_generation"
    ),
    "min_endpointing_delay": "min_endpointing_delay",
    "max_endpointing_delay": "max_endpointing_delay",
    "min_interruption_duration": "min_interruption_duration",
    "false_interruption_timeout": "false_interruption_timeout",
    "allow_interruptions": "allow_interruptions",
    "preemptive_generation": "preemptive_generation",
}
_DEPRECATED = (
    "is deprecated, and livekit-agents ignores it once turn_handling is given, as Earful "
    "gives it. Earful does not model it"
)
# AgentSession options Earful refuses, with why.
REFUSED = {
    "turn_detection": "Earful leaves turn detection to the framework's default on the scripted VAD",
    "aec_warmup_duration": (
        "would have the framework ignore the user while echo cancellation warms up, and a "
        "run has no echo: Earful turns it off"
    ),
    "use_tts_aligned_transcript": (
        "needs word timings from the voice, and the scripted voice gives none"
    ),
    "mcp_servers": "reaches an MCP server over the network",
    "video_sampler": "samples video, and Earful sends none",
    "ivr_detection": "detects a phone menu, which Earful does not model",
    "conn_options": "sets retries and timeouts of the providers, which Earful does not model",
    "loop": "is the event loop, which is Earful's virtual clock",
    "stt_context_options": "gives hints to a real speech-to-text, which the scripted one ignores",
    "keyterms_options": "gives hints to a real speech-to-text, which the scripted one ignores",
    "tool_handling": "runs tools asynchronously, which Earful does not model",
    "expressive": "asks a real voice for expressive speech",
    "discard_audio_if_uninterruptible": _DEPRECATED,
    "min_interruption_words": _DEPRECATED,
    "resume_false_interruption": _DEPRECATED,
    "agent_false_interruption_timeout": _DEPRECATED,
}

# Agent options run as the application wrote them.
AGENT_RUN = frozenset({"instructions", "id", "chat_ctx", "tools", "min_consecutive_speech_delay"})
# Agent options that land in its turn_handling, which ``agent_refusal`` reads.
AGENT_TURNS = frozenset(
    {
        "turn_handling",
        "turn_detection",
        "min_endpointing_delay",
        "max_endpointing_delay",
        "allow_interruptions",
    }
)
# Agent options refused when given, with why.
AGENT_REFUSED = {
    "stt": "would replace Earful's scripted speech-to-text",
    "vad": "would replace Earful's scripted VAD",
    "llm": "would replace Earful's scripted model",
    "tts": "would replace Earful's scripted voice",
    "mcp_servers": REFUSED["mcp_servers"],
    "tool_handling": REFUSED["tool_handling"],
    "expressive": REFUSED["expressive"],
    "use_tts_aligned_transcript": REFUSED["use_tts_aligned_transcript"],
}
# An agent's turn option the header states -> the Case field that sets it. The two must agree.
_CASE_FIELD = {
    ("endpointing", "min_delay"): "min_endpointing_delay",
    ("endpointing", "max_delay"): "max_endpointing_delay",
    ("interruption", "min_duration"): "min_interruption_duration",
    ("interruption", "false_interruption_timeout"): "false_interruption_timeout",
}
# An agent's turn options that run as the application sets them: no verdict reads them.
_AGENT_OWN = frozenset({("interruption", "enabled"), ("preemptive_generation", "enabled")})
# Turn detection values that run as the framework's default does on the scripted VAD.
_DEFAULT_TURNS = (None, "vad")


def session_options() -> frozenset[str]:
    """The options the installed release's AgentSession takes."""
    return frozenset(inspect.signature(lk.AgentSession.__init__).parameters) - {"self"}


def agent_options() -> frozenset[str]:
    """The options the installed release's Agent takes."""
    return frozenset(inspect.signature(lk.Agent.__init__).parameters) - {"self"}


def refuse_session_kwargs(given: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` in one line for the first option Earful does not pass as it is."""
    for name in sorted(given):
        if name == "llm" and (reason := realtime_provider_refusal(given[name])):
            raise ValueError(f"session_kwargs {name!r}: {reason}")
        if name == "turn_handling":
            raise ValueError(
                "session_kwargs may not set 'turn_handling': Case exposes endpointing delays, "
                "interruption duration, timeout, allow_interruptions and preemptive_generation; "
                "turn models and minimum word counts are unsupported"
            )
        if name in FROM_CASE:
            raise ValueError(f"session_kwargs may not set {name!r}: use Case.{FROM_CASE[name]}")
        if name in REFUSED:
            raise ValueError(f"session_kwargs {name!r} {REFUSED[name]}")
        passed = sorted(PASSED.keys() & session_options())
        if name not in passed:
            raise ValueError(
                f"session_kwargs {name!r} is not an AgentSession option Earful models on "
                f"livekit-agents {lk.__version__}; it passes {passed}"
            )


def _sets(option: str, value: Any) -> bool:
    if not is_given(value):
        return False
    # None replaces a provider with none; the other options are off when falsy.
    return option in ("stt", "vad", "llm", "tts", "tool_handling") or bool(value)


def agent_refusal(agent: lk.Agent, session: lk.AgentSession[Any]) -> str | None:
    """Why the run cannot model ``agent`` on ``session``, or None."""
    name = type(agent).__name__
    for option, why in AGENT_REFUSED.items():
        if option not in agent_options():
            continue
        value = seams.agent_option(agent, option)
        if _sets(option, value):
            if option == "llm" and (reason := realtime_provider_refusal(value)):
                return f"{name} sets its own llm: {reason}"
            return f"{name} sets its own {option}, which {why}"
    held: Mapping[str, Any] = session.options.turn_handling
    for group, value in seams.turn_handling(agent).items():
        if group == "turn_detection":
            if value not in _DEFAULT_TURNS:
                return (
                    f"{name} sets turn_detection={value!r}: Earful scripts the VAD, and runs "
                    "only the framework's default on it (None or 'vad')"
                )
            continue
        if not isinstance(value, Mapping):
            return f"{name} sets turn_handling {group}={value!r}, which Earful cannot read"
        for key, wanted in value.items():
            has = (held.get(group) or {}).get(key)
            if wanted == has or (group, key) in _AGENT_OWN:
                continue
            field = _CASE_FIELD.get((group, key))
            if field is not None:
                return (
                    f"{name} sets {group} {key}={wanted!r} and the case gives the session "
                    f"{has!r}: set Case.{field} to the agent's value"
                )
            return (
                f"{name} sets {group} {key}={wanted!r}, and Earful models only the session's "
                f"{has!r}"
            )
    return None


__all__ = [
    "AGENT_REFUSED",
    "AGENT_RUN",
    "AGENT_TURNS",
    "FROM_CASE",
    "PASSED",
    "REFUSED",
    "agent_options",
    "agent_refusal",
    "refuse_session_kwargs",
    "session_options",
]
