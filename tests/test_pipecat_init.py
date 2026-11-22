"""``earful init`` for a Pipecat bot: the first test it writes, and what it refuses."""

import importlib.util

import pytest

pytest.importorskip("pipecat")

from earful import pipecat as pc
from earful.cli import OK, REFUSED, main
from earful.pipecat.scaffold import scaffold


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def init(capsys, *argv):
    status = main(["init", *(str(arg) for arg in argv)])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def written(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_writes_a_test_from_the_functions_a_bot_registers(here, capsys):
    status, out, _ = init(capsys, "scaffolded_bots:booking_bot")
    assert status == OK
    assert out.splitlines() == [
        "wrote      test_booking_bot.py",
        "functions  create_reservation",
        "           each is declared an effect; delete those that only read",
        "next       pytest test_booking_bot.py",
        "           its search fails (A2) while a write has no idempotency key declared;",
        "           the file says where to declare one",
    ]
    source = (here / "test_booking_bot.py").read_text()
    assert "from scaffolded_bots import booking_bot\n" in source
    assert 'CALL = ("create_reservation", {"party_size": 1})\n' in source  # what it requires
    case = written(here / "test_booking_bot.py").booking_bot_scenario()
    assert case.bot is not None and case.name == "booking_bot"
    assert {name: effect.intent for name, effect in case.effects.items()} == {
        "create_reservation": "create-reservation"
    }


@pytest.mark.parametrize("bot", ["booking_bot", "handed_bot"])
def test_the_test_init_writes_for_a_bot_runs_as_it_is(here, capsys, bot):
    """The bots' functions fail a test that reaches their bodies."""
    assert init(capsys, f"scaffolded_bots:{bot}")[0] == OK
    module = written(here / f"test_{bot}.py")
    result = pc.run(getattr(module, f"{bot}_scenario"))
    assert module.REQUIRED == ["A2", "A3", "L1"]
    result.assert_contracts(require=module.REQUIRED)
    kinds = [event["kind"] for event in result.events]
    assert kinds.count("tool.called") == kinds.count("effect.committed") == 1


def test_a_bot_that_needs_more_than_its_model_is_handed_none_and_says_so(here, capsys):
    assert init(capsys, "scaffolded_bots:handed_bot")[0] == OK
    source = (here / "test_handed_bot.py").read_text()
    assert "    return handed_bot(llm, reservations=None)\n" in source
    assert "# handed_bot is called with: reservations.\n" in source
    assert "        bot=bot,\n" in source
    note = "# create_reservation is registered without a schema, so its arguments are not known:"
    assert note in source and 'CALL = ("create_reservation", {})\n' in source


def test_a_bot_that_registers_nothing_gets_a_test_of_its_conversation(here, capsys):
    assert init(capsys, "scaffolded_bots:greeter")[0] == OK
    greeter = written(here / "test_greeter.py")
    assert greeter.REQUIRED == ["L1"] and not greeter.greeter_scenario().effects
    pc.run(greeter.greeter_scenario).assert_contracts(require=greeter.REQUIRED)


@pytest.mark.parametrize(
    "reference, why",
    [
        ("scaffolded_bots", "a bot is named as module:function"),
        ("no_such_module:bot", "no bot 'no_such_module:bot'"),
        ("scaffolded_bots:NotABot", "is not a function"),
        ("scaffolded_bots:needs_a_service", "needs_a_service(llm, reservations=None) raised"),
    ],
)
def test_what_init_cannot_read_a_bot_from_is_refused_in_one_line(reference, why):
    with pytest.raises(ValueError, match=why.replace("(", r"\(").replace(")", r"\)")):
        scaffold(reference)


def test_a_class_named_where_livekit_agents_is_absent_is_refused_with_what_to_install(here, capsys):
    if importlib.util.find_spec("livekit") is not None:
        pytest.skip("livekit-agents is installed here")
    status, out, err = init(capsys, "scaffolded_bots:NotABot")
    assert status == REFUSED and not out
    assert err == (
        "earful: scaffolded_bots:NotABot needs livekit-agents, which is not installed. "
        'Install it with: pip install "earful[livekit]"\n'
    )
    assert not list(here.iterdir())
