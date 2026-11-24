"""The scenario header for a Pipecat pipeline."""

from __future__ import annotations

from typing import Any

from ..core.timebase import to_us
from ..scripted.case import check_intent_tools, declared_intents
from .case import Case


def _header(case: Case, tools: set[str], options: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(case.effects.keys() - tools)
    if unknown:
        raise ValueError(
            f"effects are declared for {unknown}, which are not functions the bot registers; "
            f"it registers {sorted(tools)}"
        )
    check_intent_tools(case.intents, tools)
    intents = declared_intents(case.intents, case.effects, case.speeches, case.contracts)
    return {
        "speeches": [
            {
                "speech": i,
                "start_us": sp.start_us,
                "end_us": sp.end_us,
                "transcript": sp.transcript,
                "stt_delay_us": sp.stt_delay_us,
                "expects_reply": sp.owes_reply,
                "backchannel": sp.backchannel,
                "retires": list(sp.retires),
            }
            for i, sp in enumerate(sorted(case.speeches, key=lambda sp: sp.start))
        ],
        "hangup_us": None if case.hangup is None else to_us(case.hangup),
        "intents": [
            {"intent": it.intent, "tools": list(it.tools), "authorized": it.authorized}
            for it in intents
        ],
        "witnessed_tools": sorted(tools),
        **(
            {
                "effects": [
                    {"tool": name, "intent": effect.intent}
                    for name, effect in sorted(case.effects.items())
                ]
            }
            if case.effects
            else {}
        ),
        "delays": [
            {"of": d.of, "nth": d.nth, "us": to_us(d.seconds)}
            for d in sorted(case.delays, key=lambda d: (d.of, d.nth))
        ],
        "session": {
            # Pipecat interrupts when the turn starts, which the VAD strategy does as
            # soon as the VAD reports speech: after start_secs of it, and no later.
            "min_interruption_duration_us": to_us(case.vad_start_secs),
            "false_interruption_timeout_us": None,  # Pipecat does not resume a reply
            # The turn ends user_speech_timeout after the VAD reports the end of speech.
            "min_endpointing_delay_us": to_us(case.user_speech_timeout),
            "max_endpointing_delay_us": to_us(case.user_turn_stop_timeout),
            "turn_end_delay_us": to_us(case.vad_stop_secs) + to_us(case.user_speech_timeout),
            "turn_detection": None,
            "extra_options": options,
        },
        "faults": [
            {"of": f.of, "nth": f.nth, "us": None if f.seconds is None else to_us(f.seconds)}
            for f in sorted(case.faults, key=lambda f: (f.of, f.nth))
        ],
        "providers": {
            "vad": {
                "min_speech_us": to_us(case.vad_start_secs),
                "min_silence_us": to_us(case.vad_stop_secs),
            },
            "tts": {
                "seconds_per_letter_us": to_us(case.seconds_per_letter),
                "ttfb_us": to_us(case.tts_ttfb),
                "word_timestamps": case.tts_word_timestamps,
            },
            "sink": {"can_pause": False},
            "backend": {"mode": case.backend_mode},
        },
    }
