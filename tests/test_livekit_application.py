"""A LiveKit application's session is outside the case Earful runs."""

import importlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("livekit.agents")

from earful.cli import OK, REFUSED, main
from earful.livekit import explore, run


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "session_app", raising=False)
    (tmp_path / "session_app.py").write_text(
        """
import asyncio
from asyncio import create_task as schedule
from livekit.agents import Agent, AgentSession as Session, llm
from earful.livekit import Case, ScriptedModel, UserSpeech


class Playground(Agent):
    def __init__(self):
        super().__init__(instructions="Hello")


class Separate(Agent):
    def __init__(self):
        super().__init__(instructions="Hello")


class VoiceModel(llm.RealtimeModel):
    def __init__(self):
        pass


class OtherVoice(VoiceModel):
    pass


def make_voice():
    return VoiceModel()


def make_session():
    return Session(llm=make_voice())


def maybe_voice(rooms):
    if rooms:
        return VoiceModel()


class OwnModel(Agent):
    def __init__(self):
        super().__init__(instructions="Hello", llm=VoiceModel())


class Mixed(Separate):
    pass


async def realtime():
    session = Session(llm=VoiceModel())
    await session.start(agent=Mixed())


async def cascade():
    session = Session()
    await session.start(agent=Mixed())


class Manager:
    def create(self):
        session = Session(llm=llm.RealtimeModel())
        return session

    async def connect(self):
        self.session = self.create()
        self.agent = Playground()
        await self.session.start(agent=self.agent)


def playground():
    return scenario(lambda: Playground(), scripted_turns=True)


def separate():
    return scenario(Separate)


def scripted():
    return scenario(Separate, scripted_turns=True)


def scenario(agent, **options):
    return Case(name="hello", agent=agent, **options,
                speeches=[UserSpeech(0.5, 1.5, "Hello")],
                policy=ScriptedModel().otherwise(say="Hi"))
"""
    )
    return importlib.import_module("session_app")


def test_a_realtime_application_is_refused_by_name_even_with_scripted_turns(application, capsys):
    with pytest.raises(ValueError, match="Playground is started with the speech-to-speech model"):
        run(application.playground)
    for argv in (
        ["init", "--scripted-turns", "session_app:Playground"],
        ["run", "session_app:playground", "--json"],
    ):
        assert main(argv) == REFUSED
        output = capsys.readouterr()
        assert not output.out
        assert "RealtimeModel" in output.err and len(output.err.splitlines()) == 1


def test_existing_cases_and_init_disclose_scripted_turns(application, capsys):
    assert main(["init", "session_app:Separate"]) == OK
    capsys.readouterr()
    report = run(application.separate).report
    assert report.outcome("L1") == "pass"
    assert "silence-based turn detection" in report.session_scope
    assert report.session_scope in report.lines()
    assert main(["run", "session_app:scripted", "--json"]) == OK
    assert json.loads(capsys.readouterr().out)["session_scope"] == report.session_scope
    found = explore(application.scripted, workers=0, variables=(), faults=(), words=False)
    assert report.session_scope in found.describe()
    assert found.summary()["session_scope"] == report.session_scope


def test_a_case_that_declines_scripted_turns_is_refused(application):
    with pytest.raises(ValueError, match="silence-based turn detection"):
        run(application.scenario(application.Separate, scripted_turns=False))


def test_an_agent_used_in_both_session_types_can_be_tested_on_the_scripted_cascade(application):
    assert run(application.scenario(application.Mixed, scripted_turns=True)).report.valid


def test_a_realtime_provider_on_the_agent_or_case_is_refused_by_name(application, capsys):
    with pytest.raises(ValueError, match="VoiceModel is a speech-to-speech model"):
        run(application.scenario(application.OwnModel, scripted_turns=True))
    case = application.scripted()
    case.session_kwargs["llm"] = application.VoiceModel()
    with pytest.raises(ValueError, match="VoiceModel is a speech-to-speech model"):
        run(case)
    assert main(["init", "--scripted-turns", "session_app:OwnModel"]) == REFUSED
    assert "VoiceModel is a speech-to-speech model" in capsys.readouterr().err


def test_a_livekit_run_records_the_installed_untested_release(application, monkeypatch):
    import livekit.agents as lk

    monkeypatch.setattr(lk, "__version__", "1.8.999")
    result = run(application.scripted)
    assert result.events[0]["data"]["framework"]["support"] == "untested"
    assert "Untested framework release: livekit-agents 1.8.999" in result.report.session_scope


def add_entrypoint(application, lines):
    path = Path(application.__file__)
    body = "\n".join("    " + line for line in lines)
    path.write_text(path.read_text() + "\nasync def connect_room(rooms):\n" + body + "\n")
    importlib.invalidate_caches()
    return importlib.reload(application)


DIRECT = "session = Session(llm=VoiceModel())"
START = "await session.start(agent=Separate())"


@pytest.mark.parametrize(
    "setup",
    [
        ["model = VoiceModel()", "session = Session(llm=model)", START],
        [DIRECT, "await session.start(Separate())"],
        ["session = Session[dict](llm=VoiceModel())", START],
        [DIRECT, "try:", "    " + START, "finally:", "    pass"],
        ["session = Session(llm=make_voice())", START],
        ["session = make_session()", START],
        ["session = Session(llm=make_voice() if rooms else OtherVoice())", START],
        [
            "if rooms:",
            "    model = VoiceModel()",
            "else:",
            "    model = OtherVoice()",
            "session = Session(llm=model)",
            START,
        ],
        [DIRECT, "for room in rooms:", "    " + START],
        [DIRECT, "async for room in rooms:", "    " + START],
        ["while rooms:", "    " + DIRECT, "    " + START, "    break"],
        [DIRECT, "asyncio.create_task(session.start(agent=Separate()))"],
        [DIRECT, "schedule(coro=session.start(agent=Separate()))"],
        [DIRECT, "result = " + START],
        [DIRECT, "task: asyncio.Task = asyncio.create_task(session.start(Separate()))"],
    ],
)
def test_alternative_realtime_wiring_refuses_both_init_and_run(application, capsys, setup):
    add_entrypoint(application, setup)
    for argv in (["init", "session_app:Separate"], ["run", "session_app:separate"]):
        assert main(argv) == REFUSED
        error = capsys.readouterr().err
        assert "session_app.VoiceModel" in error
        if any("OtherVoice" in line for line in setup):
            assert "session_app.OtherVoice" in error
    assert not Path("test_separate.py").exists()


@pytest.mark.parametrize(
    "setup",
    [
        ["session = Session(llm=make_voice() if rooms else None)", START],
        ["session = Session(llm=maybe_voice(rooms))", START],
        ["def make_voice():", "    return None", "session = Session(llm=make_voice())", START],
        [DIRECT, "for session in rooms:", "    " + START],
        [DIRECT, "for room in rooms:", "    break", "    " + START],
        [DIRECT, "while False:", "    " + START],
        [DIRECT, "create_task = lambda coro: None", "create_task(session.start(Separate()))"],
    ],
)
def test_ambiguous_or_unreachable_start_allows_scripted_turns(application, setup):
    application = add_entrypoint(application, setup)
    assert run(application.scripted).report.valid
