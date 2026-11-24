"""The scripted voice: each word of synthesized audio carries a stamp."""

from __future__ import annotations

from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ...core.ledger import Ledger
from ..clock import now_us
from .model import ReplyRegister

MAX_STAMP = 32767  # 0 is reserved for "no word"

# A stamp is written as three digits, one per sample, so any three samples of a word in a
# row name it. The first sample of a word is 1 + digit 0. After it the samples cycle
# through digits 1, 2 and 0, each written as 1 + _BASE * (place + 1) + digit.
_BASE = 40
_CODES = _BASE**3
# The digits are those of stamp * _STEP, so that words next to each other share no digit.
_STEP = 1 + _BASE + _BASE * _BASE
_BACK = pow(_STEP, -1, _CODES)
# The largest sample value. livekit-agents reads audio under 0.005 of full scale (163) as
# carrying no speaking rate, so a word sounds the same to it whatever its stamp.
LOUDEST = 4 * _BASE
SHORTEST = 3  # samples: a shorter word could not be told from another
_NEXT = {0: 2, 1: 2, 2: 3, 3: 1}  # the kind of sample that follows each kind


def _digits(stamp: int) -> tuple[int, int, int]:
    code = stamp * _STEP % _CODES
    return code % _BASE, code // _BASE % _BASE, code // (_BASE * _BASE)


def encode(stamp: int, samples: int) -> array[int]:
    """The audio of one word: ``samples`` samples from which its stamp can be read back."""
    digits = _digits(stamp)
    cycle = array("h", [1 + _BASE * (place + 1) + digits[place] for place in (1, 2, 0)])
    return (array("h", [1 + digits[0]]) + cycle * -(-samples // 3))[:samples]


@dataclass
class StampTable:
    """Every word the TTS ever synthesized, by stamp. Shared with the sink."""

    sample_rate: int
    words: dict[int, dict[str, Any]] = field(default_factory=dict)
    _audio: dict[int, array[int]] = field(default_factory=dict, repr=False)

    def add(self, utterance: int, index: int, text: str, samples: int) -> int:
        stamp = len(self.words) + 1
        if stamp > MAX_STAMP:
            raise OverflowError("more than 32767 words synthesized in one run")
        if samples < SHORTEST:
            raise ValueError(f"a word of {samples} audio sample(s) is too short to stamp")
        self.words[stamp] = {
            "utterance": utterance,
            "index": index,
            "text": text,
            "samples": samples,
        }
        return stamp

    def audio(self, stamp: int) -> array[int]:
        """The audio of a word in this table."""
        if stamp not in self._audio:
            self._audio[stamp] = encode(stamp, self.words[stamp]["samples"])
        return self._audio[stamp]


class StampReader:
    """Reads stamps back out of audio, in the order the samples are heard."""

    def __init__(self, table: StampTable) -> None:
        self._table = table
        self._stamp = 0  # the word being read; 0 between words
        self._at = 0  # samples of it read so far; -1 if it was joined part way
        self._kind = 0  # the kind of sample due next, for a word joined part way
        self._last = 0  # the last word named
        self._whole = True  # whether that word was read to its end
        self._held: list[int] = []  # the first samples of a word not yet named
        self.errors = 0  # samples that belong to no word in the table

    def read(self, samples: Sequence[int]) -> list[tuple[int, int]]:
        """``samples`` as (stamp, count) runs. Stamp 0 is silence or an unreadable sample.
        Up to two samples at the start of a word are held until the next call or ``end``."""
        if not isinstance(samples, (array, memoryview)):
            samples = array("h", samples)
        runs: list[tuple[int, int]] = []
        i, n = 0, len(samples)
        while i < n:
            value = samples[i]
            if self._held:
                if 1 <= value <= LOUDEST and _kind(value) == _NEXT[_kind(self._held[-1])]:
                    self._held.append(value)
                    i += 1
                    self._name(runs)
                else:
                    self._settle(runs)
            elif value == 0:
                self._stamp = 0
                _put(runs, 0, 1)
                i += 1
            elif self._stamp and self._at >= 0:
                stamp, audio = self._stamp, self._table.audio(self._stamp)
                take = min(len(audio) - self._at, n - i)
                if not take or samples[i : i + take] != audio[self._at : self._at + take]:
                    same = 0
                    while same < take and samples[i + same] == audio[self._at + same]:
                        same += 1
                    take, self._stamp = same, 0
                if take:
                    _put(runs, stamp, take)
                    self._at += take
                    i += take
                self._whole = self._at == len(audio)
            elif self._stamp and value == _sample(self._stamp, self._kind):
                _put(runs, self._stamp, 1)
                self._kind = _NEXT[self._kind]
                i += 1
            else:
                self._stamp = 0
                self._held = [value]
                i += 1
                if not 1 <= value <= LOUDEST:
                    self._settle(runs)
        return runs

    def end(self) -> list[tuple[int, int]]:
        """Whatever was still held when the audio stopped, as runs."""
        runs: list[tuple[int, int]] = []
        self._settle(runs)
        self._stamp = 0
        return runs

    def _known(self) -> dict[int, int]:
        """The digits the held samples show, by place. Empty if they disagree."""
        known: dict[int, int] = {}
        for value in self._held:
            place, digit = max(_kind(value) - 1, 0), (value - 1) % _BASE
            if not 1 <= value <= LOUDEST or known.setdefault(place, digit) != digit:
                return {}
        return known

    def _name(self, runs: list[tuple[int, int]]) -> None:
        known = self._known()
        if len(known) == 3:
            code = known[0] + _BASE * known[1] + _BASE * _BASE * known[2]
            stamp = code * _BACK % _CODES
            if stamp in self._table.words:
                self._start(runs, stamp)
                return
        if len(known) in (0, 3):
            self._settle(runs)

    def _start(self, runs: list[tuple[int, int]], stamp: int) -> None:
        _put(runs, stamp, len(self._held))
        from_its_start = _kind(self._held[0]) == 0
        self._stamp = self._last = stamp
        self._at = len(self._held) if from_its_start else -1
        self._kind = _NEXT[_kind(self._held[-1])]
        self._whole = False
        self._held = []

    def _settle(self, runs: list[tuple[int, int]]) -> None:
        """Name a word heard for fewer samples than name it, if only one word can be meant:
        the one that follows a word read to its end, or the only one with these digits."""
        if not self._held:
            return
        known = self._known()
        fits = [
            stamp
            for stamp in self._table.words
            if known and all(_digits(stamp)[place] == digit for place, digit in known.items())
        ]
        if self._whole and _kind(self._held[0]) == 0 and self._last + 1 in fits:
            fits = [self._last + 1]
        if len(fits) == 1:
            self._start(runs, fits[0])
            self._stamp = 0
        else:
            self.errors += len(self._held)
            _put(runs, 0, len(self._held))
            self._held = []
            self._whole = False


def _kind(value: int) -> int:
    """0 for the first sample of a word, else 1 + the place of the digit it carries."""
    return (value - 1) // _BASE


def _sample(stamp: int, kind: int) -> int:
    return 1 + _BASE * kind + _digits(stamp)[kind - 1]


def _put(runs: list[tuple[int, int]], stamp: int, count: int) -> None:
    if runs and runs[-1][0] == stamp:
        runs[-1] = (stamp, runs[-1][1] + count)
    else:
        runs.append((stamp, count))


def words_heard(
    runs: Sequence[Sequence[int]], heard: int, table: StampTable
) -> tuple[list[int], list[int], int | None, int]:
    """Of ``runs`` in the order written, what the first ``heard`` samples hold: the words
    played in full, where each ended (samples from the start), the word cut and its samples."""
    consumed: dict[int, int] = {}
    ended: dict[int, int] = {}
    offset = 0
    for stamp, count in runs:
        take = max(0, min(count, heard - offset))
        if stamp and take:
            consumed[stamp] = consumed.get(stamp, 0) + take
            ended[stamp] = offset + take
        offset += count
    played = [stamp for stamp, got in consumed.items() if got >= table.words[stamp]["samples"]]
    cut = [stamp for stamp in consumed if stamp not in played]
    return (
        played,
        [ended[stamp] for stamp in played],
        cut[-1] if cut else None,
        consumed[cut[-1]] if cut else 0,
    )


def words_altered(runs: Sequence[Sequence[int]], table: StampTable) -> int:
    """How many words of ``runs`` were written with other than their audio: more of it, which
    only a repeat gives, or less, other than the last one written, whose synthesis was cut."""
    written: dict[int, int] = {}
    for stamp, count in runs:
        if stamp:
            written[stamp] = written.get(stamp, 0) + count
    short = [stamp for stamp, got in written.items() if got < table.words[stamp]["samples"]]
    repeated = sum(got > table.words[stamp]["samples"] for stamp, got in written.items())
    return repeated + len(short) - (1 if short and short[-1] == list(written)[-1] else 0)


def letters(word: str) -> int:
    """How long a word is to say, counted in letters and digits; never less than one."""
    return sum(ch.isalnum() for ch in word) or 1


@dataclass
class Synthesis:
    number: int
    ledger: Ledger
    table: StampTable
    register: ReplyRegister
    sample_rate: int
    letter_us: int
    ttfb_us: int
    started_us: int = field(init=False)
    t0_us: int = field(init=False)
    eligible: set[int] = field(init=False)
    words: list[str] = field(default_factory=list)
    stamps: list[int] = field(default_factory=list)
    word_samples: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.started_us = self.ledger.now_us()
        self.t0_us = now_us()
        self.eligible = set(self.register.replies)

    def render(self, word: str) -> bytes:
        samples = (self.sample_rate * letters(word) * self.letter_us + 500_000) // 1_000_000
        stamp = self.table.add(self.number, len(self.words), word, samples)
        self.words.append(word)
        self.stamps.append(stamp)
        self.word_samples.append(samples)
        return self.table.audio(stamp).tobytes()

    def finish(self) -> None:
        text = " ".join(self.words)
        call, attribution, candidates = self.register.attribute(text, eligible=self.eligible)
        self.ledger.append(
            "world",
            "tts.synthesized",
            utterance=self.number,
            call=call,
            attribution=attribution,
            candidates=candidates,
            text=text,
            stamps=self.stamps,
            word_samples=self.word_samples,
            sample_rate=self.sample_rate,
            ttfb_us=self.ttfb_us,
            started_us=self.started_us,
        )
