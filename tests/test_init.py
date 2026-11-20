"""``earful init``: the first test it writes for an agent, and what it refuses."""

import importlib.util
import subprocess

import pytest

from earful.cli import OK, REFUSED, main
from earful.core.checker import ContractFailure
from earful.livekit import Effect, Fault, run
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
        "           its search fails (A2) while a write has no idempotency key declared;",
        "           the file says where to declare one",
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
        assert module.REQUIRED == ["A2", "A3", "L1"]
        result.assert_contracts(require=module.REQUIRED)
        kinds = [event["kind"] for event in result.events]
        assert kinds.count("tool.called") == kinds.count("effect.committed") == 1
        (called,) = (e["data"] for e in result.events if e["kind"] == "tool.called")
        assert called["name"] == tool
    assert init(capsys, "scaffolded:Greeter")[0] == OK
    greeter = written(here / "test_greeter.py")
    assert not greeter.greeter().effects and "Effect" not in (here / "test_greeter.py").read_text()
    assert greeter.REQUIRED == ["L1"]
    run(greeter.greeter).assert_contracts(require=greeter.REQUIRED)
    source = (here / "test_greeter.py").read_text()
    assert source.count(".assert_contracts(require=REQUIRED)\n") == 2


def test_the_test_init_writes_fails_on_a_write_the_model_repeats_without_a_key(here, capsys):
    assert init(capsys, "scaffolded:BookingAgent")[0] == OK
    module = written(here / "test_booking_agent.py")

    def timed_out(keyed):
        """The first write commits and its caller is told it timed out, as a search tries."""
        case = module.booking_agent()
        case.faults = [Fault("backend.timeout", 1, 1.0)]
        if keyed:  # the change the written file names
            case.effects = {"create_reservation": Effect("create-reservation", key="table")}
            case.backend_mode = "keyed"
        return case

    result = run(timed_out(keyed=False))
    with pytest.raises(ContractFailure, match="2 durable effects for 'create-reservation'"):
        result.assert_contracts(require=module.REQUIRED)
    assert [f.rule for v in result.report.results["A2"].failures for f in v.findings] == ["rule"]
    run(timed_out(keyed=True)).assert_contracts(require=module.REQUIRED)
    source = (here / "test_booking_agent.py").read_text()
    assert 'Effect(..., key=...) here, and\n        # backend_mode="keyed" on this Case.' in source


def test_the_model_calls_a_tool_with_a_value_for_each_argument_the_tool_requires():
    """An argument that has a default is left out."""
    source = scaffold("scaffolded:SupportAgent").source
    assert 'CALL = ("issue_refund", {"order": "example", "amount": 1})\n' in source
    assert 'claims=["issue-refund"]' in source
    from scaffolded import SupportAgent

    from earful.livekit.scaffold import _arguments

    (look_up,) = (tool for tool in SupportAgent().tools if tool.info.name == "look_up")
    assert _arguments(look_up) == {"order_id": 1, "how": "card", "tags": [], "note": "example"}


def test_a_model_a_date_and_an_identifier_each_get_a_value_the_tool_accepts(here, capsys):
    assert init(capsys, "scaffolded:TravelAgent")[0] == OK
    source = (here / "test_travel_agent.py").read_text()
    assert max(len(line) for line in source.splitlines()) <= 100
    module = written(here / "test_travel_agent.py")
    assert module.CALL == (
        "book_trip",
        {
            "trip": {
                "traveller": "example",
                "stops": [{"city": "example"}],
                "leaves": "2026-01-01T12:00:00Z",
            },
            "on": "2026-01-01",
            "reference": "00000000-0000-0000-0000-000000000000",
        },
    )
    result = run(module.travel_agent)
    result.assert_contracts(require=module.REQUIRED)
    kinds = [event["kind"] for event in result.events]
    assert kinds.count("effect.committed") == kinds.count("tool.returned") == 1


@pytest.mark.parametrize(
    "agent, why",
    [
        (
            "TakesACode",
            "`earful init` cannot write a call of the tool 'redeem': it has no value for the "
            + "argument 'voucher.code' that the tool accepts (String should match pattern",
        ),
        (
            "DeclaresAPattern",
            "`earful init` cannot write a call of the tool 'redeem_coded': it has no value for "
            + "the argument 'code' that the tool accepts ('example' does not match",
        ),
        (
            "CannotBeBuilt",
            "CannotBeBuilt() raised RuntimeError: no configuration. `earful init` builds the "
            + "agent to read its tools",
        ),
    ],
)
def test_an_agent_init_cannot_write_a_test_for_is_refused_in_one_line(here, capsys, agent, why):
    status, out, err = init(capsys, f"scaffolded:{agent}")
    assert status == REFUSED and not out and not list(here.iterdir())
    assert err.startswith(f"earful: {why}") and err.count("\n") == 1


def test_a_tool_that_declares_its_own_schema_is_called_with_what_the_schema_takes(here, capsys):
    assert init(capsys, "scaffolded:DeclaresASchema")[0] == OK
    module = written(here / "test_declares_a_schema.py")
    assert module.CALL == ("redeem_plain", {"code": "example"})
    run(module.declares_a_schema).assert_contracts(require=module.REQUIRED)


def test_the_model_calls_the_first_tool_a_call_can_be_written_for(here, capsys):
    made = scaffold("scaffolded:DeclaresBoth")
    assert made.tools == ("redeem_coded", "redeem_plain")
    assert 'CALL = ("redeem_plain", {"code": "example"})\n' in made.source
    assert '"redeem_coded": Effect("redeem-coded"),' in made.source


def test_tools_handed_to_the_constructor_are_found(here, capsys):
    made = scaffold("scaffolded:HandedItsTools")
    assert made.tools == ("send_invoice",) and made.unbuilt is None
    assert init(capsys, "scaffolded:HandedItsTools")[0] == OK
    module = written(here / "test_handed_its_tools.py")
    run(module.handed_its_tools).assert_contracts(require=module.REQUIRED)


def test_an_agent_that_none_does_not_build_gets_a_test_that_says_so(here, capsys):
    status, out, _ = init(capsys, "scaffolded:NeedsItsService")
    assert status == OK
    assert out.splitlines()[3:5] == [
        "build      None for each argument does not build the agent "
        + "(ValueError: a service is needed)",
        "           give build() in the file a stand-in for each before running it",
    ]
    source = (here / "test_needs_its_service.py").read_text()
    assert "    # None does not build it: give a stand-in for each, and for what a\n" in source
    assert '"cancel_order": Effect("cancel-order"),' in source  # read from the class
    assert "does not build" not in init(capsys, "scaffolded:BookingAgent")[1]


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
        ("json:JSONDecoder", "writes a test for a livekit-agents agent only"),
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
