"""The scripted voice: each word of synthesized audio carries a stamp."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_STAMP = 32767  # int16; 0 is reserved for "no word"


@dataclass
class StampTable:
    """Every word the TTS ever synthesized, by stamp. Shared with the sink."""

    sample_rate: int
    words: dict[int, dict[str, Any]] = field(default_factory=dict)

    def add(self, utterance: int, index: int, text: str, samples: int) -> int:
        stamp = len(self.words) + 1
        if stamp > MAX_STAMP:
            raise OverflowError("more than 32767 words synthesized in one run")
        self.words[stamp] = {
            "utterance": utterance,
            "index": index,
            "text": text,
            "samples": samples,
        }
        return stamp


def letters(word: str) -> int:
    """How long a word is to say, counted in letters and digits; never less than one."""
    return sum(ch.isalnum() for ch in word) or 1
