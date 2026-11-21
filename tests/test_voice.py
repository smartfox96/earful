"""The stamp a word's audio carries, and reading it back from what was heard."""

import random
from array import array

import pytest

from earful.scripted.voice import (
    LOUDEST,
    SHORTEST,
    StampReader,
    StampTable,
    encode,
    words_heard,
    words_missing_audio,
)


def _table(*lengths):
    table = StampTable(sample_rate=24000)
    for index, samples in enumerate(lengths):
        table.add(1, index, f"w{index + 1}", samples)
    return table


def _read(table, pcm, frame=240):
    """The stamp of each sample of ``pcm``, read ``frame`` samples at a time, and the errors."""
    reader, stamps = StampReader(table), []
    for start in range(0, len(pcm), frame):
        for stamp, count in reader.read(pcm[start : start + frame]):
            stamps += [stamp] * count
    for stamp, count in reader.end():
        stamps += [stamp] * count
    return stamps, reader.errors


def test_every_sample_is_under_the_level_livekit_reads_as_silence():
    assert LOUDEST < 0.005 * 32767
    for stamp in (1, 39, 40, 1599, 1600, 32767):
        audio = encode(stamp, 50)
        assert len(audio) == 50 and 0 < min(audio) and max(audio) <= LOUDEST


def test_words_next_to_each_other_share_no_sample_value():
    for stamp in range(1, 4000):
        assert all(a != b for a, b in zip(encode(stamp, 4), encode(stamp + 1, 4), strict=True))


def test_audio_is_read_back_the_same_whatever_the_frames():
    rng = random.Random(3)
    for _ in range(300):
        table = _table(
            *(rng.choice([3, 4, 5, 7, 50, 333, 1440]) for _ in range(rng.randint(1, 900)))
        )
        pcm, truth, word = array("h"), [], rng.randint(1, len(table.words))
        for _ in range(rng.randint(1, 12)):
            if rng.random() < 0.15:
                silence = rng.randint(1, 7)
                pcm += array("h", [0] * silence)
                truth += [0] * silence
            elif rng.random() < 0.2:
                word = rng.randint(1, len(table.words))  # the output skips to another word
            if word > len(table.words):
                break
            pcm += table.audio(word)
            truth += [word] * len(table.audio(word))
            word += 1
        stamps, errors = _read(table, pcm, frame=rng.choice([1, 2, 3, 5, 240, 960]))
        assert stamps == truth and errors == 0


def test_three_samples_in_a_row_name_a_word_joined_part_way():
    table = _table(*[100] * 3300)  # enough words that two samples fit more than one
    for skipped in (1, 2, 3, 50, 97):
        stamps, errors = _read(table, table.audio(41)[skipped:] + table.audio(42))
        assert stamps == [41] * (100 - skipped) + [42] * 100 and errors == 0


def test_a_word_cut_after_two_samples_is_the_one_that_follows_a_word_read_to_its_end():
    table = _table(*[100] * 90)
    stamps, errors = _read(table, table.audio(7) + table.audio(8)[:2])
    assert stamps == [7] * 100 + [8, 8] and errors == 0
    # Word 47 starts with the same sample as word 7 and does not follow word 6.
    stamps, errors = _read(table, table.audio(6)[:50] + table.audio(47)[:1])
    assert stamps == [6] * 50 + [0] and errors == 1


def test_samples_that_fit_no_one_word_are_unreadable():
    table = _table(*[100] * 90)
    assert _read(table, table.audio(47)[60:61]) == ([0], 1)  # one sample from inside a word
    assert _read(table, array("h", [LOUDEST + 1])) == ([0], 1)
    assert _read(table, array("h", [-5])) == ([0], 1)
    assert _read(table, encode(91, 100)) == ([0] * 100, 100)  # a stamp never given out
    alone = _table(100)
    assert _read(alone, alone.audio(1)[60:61]) == ([1], 0)  # only one word it can be


def test_a_word_too_short_to_stamp_is_refused():
    table = _table(SHORTEST)
    with pytest.raises(ValueError, match="too short to stamp"):
        table.add(1, 1, "a", SHORTEST - 1)


def test_a_word_is_played_once_its_last_sample_is_heard():
    table = _table(100, 100, 100)
    runs = [[1, 100], [0, 10], [2, 100], [3, 100]]
    assert words_heard(runs, 209, table) == ([1], [100], 2, 99)
    assert words_heard(runs, 210, table) == ([1, 2], [100, 210], None, 0)
    assert words_heard(runs, 0, table) == ([], [], None, 0)


def test_only_the_last_word_written_may_have_less_than_its_audio():
    table = _table(100, 100, 100)
    assert words_missing_audio([[1, 100], [0, 10], [2, 100], [3, 100]], table) == 0
    assert words_missing_audio([[1, 100], [2, 100], [3, 99]], table) == 0  # synthesis was cut
    assert words_missing_audio([[1, 100], [2, 99], [3, 100]], table) == 1
    assert words_missing_audio([[1, 99], [2, 99], [3, 99]], table) == 2
    assert words_missing_audio([[1, 60], [0, 10], [1, 40], [2, 100]], table) == 0  # in two parts
    assert words_missing_audio([], table) == 0
