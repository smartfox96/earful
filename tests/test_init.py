"""``earful init``: the first test it writes for an agent, and what it refuses."""

import importlib.util
import subprocess

import pytest

from earful.cli import OK, REFUSED, main
from earful.livekit import run
from earful.livekit.scaffold import scaffold


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def init(capsys, *argv):
    status = main(["init", *(str(arg) for arg in argv)])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def written(path):
    """The test file at ``path``, imported as a module."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_writes_a_test_from_the_agents_tools_and_says_what_to_do_next(here, capsys):
    status, out, _ = init(capsys, "scaffolded:SupportAgent")
    assert status == OK
    assert out.splitlines() == [
        "wrote      test_support_agent.py",
        "tools      issue_refund, look_up",
        "           each is declared an effect; delete those that only read",
        "next       pytest test_support_agent.py",
    ]
    source = (here / "test_support_agent.py").read_text()
    assert "from scaffolded import SupportAgent\n" in source
    # Every tool is an effect, so no tool body runs until the author says which may.
    case = written(here / "test_support_agent.py").support_agent()
    assert {tool: effect.intent for tool, effect in case.effects.items()} == {
        "issue_refund": "issue-refund",
        "look_up": "look-up",
    }
    assert case.name == "support_agent"


def test_the_test_init_writes_runs_as_it_is(here, capsys):
    """The tools of these agents fail a test that reaches their bodies."""
    for agent, tool in (("SupportAgent", "issue_refund"), ("BookingAgent", "create_reservation")):
        assert init(capsys, f"scaffolded:{agent}")[0] == OK
        module = written(here / scaffold(f"scaffolded:{agent}").filename)
        scenario = getattr(module, scaffold(f"scaffolded:{agent}").filename[5:-3])
        result = run(scenario)
        result.assert_contracts()
        kinds = [event["kind"] for event in result.events]
        assert kinds.count("tool.called") == kinds.count("effect.committed") == 1
        (called,) = (e["data"] for e in result.events if e["kind"] == "tool.called")
        assert called["name"] == tool
    assert init(capsys, "scaffolded:Greeter")[0] == OK
    greeter = written(here / "test_greeter.py")
    assert not greeter.greeter().effects and "Effect" not in (here / "test_greeter.py").read_text()
    run(greeter.greeter).assert_contracts()


def test_the_model_calls_a_tool_with_a_value_for_each_argument_the_tool_requires():
    """An argument that has a default is left out."""
    source = scaffold("scaffolded:SupportAgent").source
    assert 'call=("issue_refund", {"order": "example", "amount": 1}),' in source
    assert 'claims=["issue-refund"]' in source
    from scaffolded import SupportAgent

    from earful.livekit.scaffold import _arguments

    (look_up,) = (tool for tool in SupportAgent().tools if tool.info.name == "look_up")
    assert _arguments(look_up) == {"order_id": 1, "how": "card", "tags": [], "note": "example"}


def test_an_agent_that_cannot_be_built_without_arguments_is_built_with_none_for_each():
    made = scaffold("scaffolded:BookingAgent")
    assert made.tools == ("create_reservation",)  # found without building the agent
    assert "    return BookingAgent(None, greeting=None)\n" in made.source
    assert "        agent=build,\n" in made.source
    assert "is built with: reservations, greeting." in made.source
    assert "agent=SupportAgent," in scaffold("scaffolded:SupportAgent").source


def test_init_does_not_write_over_a_file_and_writes_where_it_is_told(here, capsys):
    (here / "test_greeter.py").write_text("mine\n")
    status, out, err = init(capsys, "scaffolded:Greeter")
    assert status == REFUSED and not out
    assert "test_greeter.py is already there" in err
    assert (here / "test_greeter.py").read_text() == "mine\n"
    status, out, _ = init(capsys, "scaffolded:Greeter", "-o", "tests/first.py")
    assert status == REFUSED  # the directory is the author's to make
    (here / "tests").mkdir()
    status, out, _ = init(capsys, "scaffolded:Greeter", "-o", "tests/first.py")
    assert status == OK and out.splitlines()[0] == "wrote      tests/first.py"
    assert out.splitlines()[-1] == "next       pytest tests/first.py"
    compile((here / "tests" / "first.py").read_text(), "first.py", "exec")


@pytest.mark.parametrize(
    "reference, why",
    [
        ("scaffolded", "is named as module:Class"),
        ("scaffolded:SupportAgent.look_up", "is named as module:Class"),
        ("no_such_module:Agent", "no agent 'no_such_module:Agent'"),
        ("scaffolded:Missing", "no agent 'scaffolded:Missing'"),
        ("scaffolded:not_an_agent", "is not a class of agent"),
        ("json:JSONDecoder", "is not a class of agent"),
    ],
)
def test_what_does_not_name_a_class_of_agent_is_refused(here, capsys, reference, why):
    status, out, err = init(capsys, reference)
    assert status == REFUSED and not out and why in err
    assert not list(here.iterdir())


def test_init_sees_that_git_leaves_the_evidence_out(here, capsys):
    assert "ignored" not in init(capsys, "scaffolded:Greeter", "-o", "a.py")[1]
    assert not (here / ".gitignore").exists()
    # The last line of this .gitignore has no newline: the entry still gets its own line.
    (here / ".gitignore").write_text("dist/\n*.pyc")
    assert (
        "ignored    .earful/ (added to .gitignore)"
        in init(capsys, "scaffolded:Greeter", "-o", "b.py")[1].splitlines()
    )
    assert (here / ".gitignore").read_text() == "dist/\n*.pyc\n.earful/\n"
    assert "ignored" not in init(capsys, "scaffolded:Greeter", "-o", "c.py")[1]
    assert (here / ".gitignore").read_text() == "dist/\n*.pyc\n.earful/\n"


def test_in_a_repository_with_no_list_of_what_to_leave_out_init_writes_one(here, capsys):
    subprocess.run(["git", "init", "-q", "."], check=True, cwd=here)
    out = init(capsys, "scaffolded:Greeter")[1]
    assert "ignored    .earful/ (.gitignore written)" in out.splitlines()
    assert (here / ".gitignore").read_text() == ".earful/\n"
