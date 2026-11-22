"""Every case must produce the same normalized ledger on every run."""

import logging

import pytest

pytest.importorskip("livekit.agents")

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case
from earful.scripted.playbook import tool_returned


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_case_same_ledger(name):
    runs = [run_case(CASES[name](), seed=0) for _ in range(5)]
    assert len({r.hash for r in runs}) == 1
    for run in runs:
        assert run.invalid == []
        assert run.framework_warnings == []


def test_the_environment_cannot_switch_interruption_to_a_model_over_the_network(monkeypatch):
    """With these set, livekit-agents 1.8 builds an interruption detector that is a client."""
    plain = run_case(CASES["barge_in_mid_word"]()).hash
    monkeypatch.setenv("LIVEKIT_DEV_MODE", "1")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    monkeypatch.setenv("LIVEKIT_INFERENCE_URL", "http://127.0.0.1:9")  # nothing listens there
    assert run_case(CASES["barge_in_mid_word"]()).hash == plain


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_logging_level_does_not_change_the_ledger(name, caplog):
    """A log record reads the clock when it is made, and a lower level makes more of them."""
    plain = run_case(CASES[name]()).hash
    with caplog.at_level(logging.DEBUG, logger="livekit.agents"):
        assert run_case(CASES[name]()).hash == plain
    assert caplog.records


class _Warns(lk.Agent):
    def __init__(self):
        super().__init__(instructions="t")

    @lk.function_tool
    async def look_up(self) -> str:
        """Look it up."""
        logging.getLogger("livekit.agents.voice").warning("the framework warns")
        return "found"


def _warns():
    return Case(
        name="warns",
        agent=_Warns,
        speeches=[UserSpeech(0.5, 1.5, "Look it up.")],
        policy=ScriptedModel().when(tool_returned(), say="Found.").otherwise(call=("look_up", {})),
    )


class _Seen(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        if record.name.startswith("livekit"):
            self.records.append(record.getMessage())


@pytest.mark.parametrize(
    ("levels", "shown"),
    [
        ({}, True),
        ({"livekit.agents": logging.ERROR}, False),
        ({"livekit.agents.voice": logging.CRITICAL}, False),
        ({"livekit.agents": logging.ERROR, "livekit.agents.voice": logging.INFO}, True),
    ],
)
def test_a_framework_warning_is_counted_whatever_its_logger_lets_through(levels, shown):
    """The application's own handlers see what they would have; the ledger counts it all."""
    seen, root = _Seen(), logging.getLogger()
    named = {name: logging.getLogger(name) for name in ("livekit.agents", "livekit.agents.voice")}
    before = {name: (one.level, list(one.handlers), one.propagate) for name, one in named.items()}
    root.addHandler(seen)
    try:
        for name, level in levels.items():
            named[name].setLevel(level)
        run = run_case(_warns())
        after = {
            name: (one.level, list(one.handlers), one.propagate) for name, one in named.items()
        }
    finally:
        root.removeHandler(seen)
        for name, (level, _handlers, _propagate) in before.items():
            named[name].setLevel(level)
    assert run.framework_warnings == ["livekit.agents.voice: the framework warns"]
    assert run.ledger.events[-1]["data"]["framework_warnings"] == 1
    assert seen.records == ["the framework warns"] * shown
    expected = {name: (levels.get(name, level), h, p) for name, (level, h, p) in before.items()}
    assert after == expected  # every level, handler and propagate flag is as it was
