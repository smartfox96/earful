"""A LiveKit application's session is outside the case Earful runs."""

import importlib
import json
import sys

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


@pytest.mark.parametrize("wiring", ["variable", "positional", "generic", "try"])
def test_alternative_realtime_wiring_refuses_both_init_and_run(application, capsys, wiring):
    from pathlib import Path

    path = Path(application.__file__)
    direct = "session = Session(llm=VoiceModel())"
    keyword = "await session.start(agent=Separate())"
    setup = {
        "variable": ["model = VoiceModel()", "session = Session(llm=model)", keyword],
        "positional": [direct, "await session.start(Separate())"],
        "generic": ["session = Session[dict](llm=VoiceModel())", keyword],
        "try": [direct, "try:", "    " + keyword, "finally:", "    pass"],
    }[wiring]
    body = "\n".join("    " + line for line in setup)
    path.write_text(path.read_text() + "\nasync def connect_room():\n" + body + "\n")
    importlib.invalidate_caches()
    application = importlib.reload(application)
    for argv in (["init", "session_app:Separate"], ["run", "session_app:separate"]):
        assert main(argv) == REFUSED
        assert "session_app.VoiceModel" in capsys.readouterr().err
    assert not Path("test_separate.py").exists()
