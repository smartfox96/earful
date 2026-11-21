"""The framework release an adapter accepts, warns about or refuses at import."""

import warnings

import pytest

from earful.scripted import scenario
from earful.scripted.releases import SUPPORT, UntestedRelease, admit


def _warned(package, installed):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        admit(package, installed)
    return [str(w.message) for w in caught if w.category is UntestedRelease]


@pytest.mark.parametrize("package", SUPPORT)
def test_a_tested_release_is_admitted_without_a_word(package):
    assert SUPPORT[package].tested
    for release in SUPPORT[package].tested:
        assert _warned(package, release) == []


def test_a_release_inside_the_range_and_not_tested_is_warned_about():
    said = (
        "Earful is not tested on livekit-agents 1.8.999. "
        "The tested releases are: 1.5.6, 1.5.7, 1.6.5, 1.6.6, 1.8.12, 1.8.13"
    )
    assert _warned("livekit-agents", "1.8.999") == [said]
    assert len(_warned("livekit-agents", "1.5.6rc1")) == 1
    assert len(_warned("pipecat-ai", "1.16")) == 1
    assert len(_warned("pipecat-ai", "1.16.1")) == 1


@pytest.mark.parametrize(
    ("package", "installed"),
    [
        ("livekit-agents", "1.5.5"),
        ("livekit-agents", "1.9"),
        ("livekit-agents", "1.9.0"),
        ("livekit-agents", "1.9.0rc1"),
        ("livekit-agents", "2.0.0b1"),
        ("livekit-agents", "unknown"),
        ("pipecat-ai", "1.15.9"),
        ("pipecat-ai", "1.17.0"),
    ],
)
def test_a_release_outside_the_range_is_refused_in_one_line(package, installed):
    with pytest.raises(ImportError) as refused:
        admit(package, installed)
    said = str(refused.value)
    support = SUPPORT[package]
    assert "\n" not in said and f"and {installed} is installed" in said
    assert f"from {support.lowest} up to, and not including, {support.below}" in said
    assert said.endswith(f'pip install "earful[{support.extra}]"')


def _filters():
    return [(action, category.__name__) for action, _, category, _, _ in warnings.filters]


def test_a_worker_process_does_not_repeat_the_warning():
    with scenario.pool(1) as workers:
        assert ("ignore", "UntestedRelease") in workers.submit(_filters).result()
