"""The pages agree with the LiveKit adapter: its settings, its names and the searches they quote."""

import ast
import inspect
import pickle
import pydoc
import re
import subprocess
import sys

import pytest

pytest.importorskip("livekit.agents")

from test_docs import (
    README,
    REPORT_DOC,
    ROOT,
    SEARCH_MENTIONED,
    TESTING_DOC,
    _commands,
    _stubs,
)

from earful.scripted.releases import SUPPORT


def test_every_setting_the_livekit_adapter_has_is_documented():
    from earful.livekit.scenario import SETTINGS

    assert set(SETTINGS) <= SEARCH_MENTIONED, sorted(set(SETTINGS) - SEARCH_MENTIONED)


def test_every_name_the_testing_page_imports_from_earful_exists():
    import ast
    import importlib

    blocks = re.findall(r"```python\n(.*?)```", TESTING_DOC + REPORT_DOC, flags=re.DOTALL)
    assert blocks
    imported = []
    for block in blocks:
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("earful"):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{node.module} has no {alias.name}"
                    imported.append(alias.name)
    assert {"run", "explore", "keep", "replay", "ScriptedModel", "Effect", "effect"} <= set(
        imported
    )
    assert {"render", "render_bundle"} <= set(imported)


def test_help_shows_each_public_function_as_a_function_of_the_adapter():
    import earful.livekit as adapter

    for name in ("run", "explore", "cross_check", "keep", "replay"):
        function = getattr(adapter, name)
        shown = pydoc.render_doc(function, renderer=pydoc.plaintext)
        said = f"Python Library Documentation: function {name} in module earful.livekit.api\n"
        assert inspect.isfunction(function) and shown.startswith(said) and function.__doc__
        assert pickle.loads(pickle.dumps(function)) is function
    assert str(inspect.signature(adapter.run)) == (
        "(source: 'str | Case | Callable[[], Case]', *, "
        "data: 'dict[str, Any] | None' = None, seed: 'int | None' = None) -> 'RunResult'"
    )


def test_the_old_import_paths_of_the_clock_and_the_scripted_model_give_the_same_objects():
    from earful.livekit import clock, playbook
    from earful.scripted import clock as shared_clock
    from earful.scripted import playbook as shared_playbook

    for old, shared in ((clock, shared_clock), (playbook, shared_playbook)):
        assert old.__all__ and "earful.scripted" in old.__doc__
        assert all(getattr(old, name) is getattr(shared, name) for name in old.__all__)


def _newest_tested_release():
    import livekit.agents as lk

    if lk.__version__ != SUPPORT["livekit-agents"].tested[-1]:
        pytest.skip("the pages quote a search on the newest tested release")
    return lk.__version__


def test_the_readme_and_the_search_page_quote_the_cases_the_example_search_plans():
    release = _newest_tested_release()
    done = subprocess.run(
        [sys.executable, "-m", "earful", "search", "test_booking:booking"]
        + ["--max-runs", "1", "--workers", "0"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT / "examples" / "booking",
    )
    (planned,) = re.findall(r"^(earful: (\d+) cases .*)$", done.stderr, re.MULTILINE)
    line, cases = planned
    line = re.sub(r"\. The budget of 1 runs covers 0 of them", "", line)
    assert f"```\n{line}\n```" in (ROOT / "docs" / "search.md").read_text()
    assert f"booking on livekit-agents {release}: {cases} cases tried, {cases} judged" in README
    assert f"Earful ran the conversation {cases} times" in README


def test_the_testing_page_quotes_the_cases_its_search_plans():
    from test_inline import CONFIRMS_ANYTHING, booking

    from earful.livekit import explore

    release = _newest_tested_release()
    plans = []
    explore(booking(CONFIRMS_ANYTHING), planned=plans.append, max_runs=1)
    page, cases = (ROOT / "docs" / "testing.md").read_text(), plans[0].cases
    assert (
        f"inline_booking on livekit-agents {release}: {cases} cases tried, {cases} judged" in page
    )
    assert f"The default search above is {cases} cases." in page
    assert f"the {cases} cases of section 5 take" in " ".join(page.split())


SKILL = (ROOT / "skills" / "earful" / "SKILL.md").read_text()


def test_the_skill_for_coding_agents_uses_only_what_exists():
    """The skill's code is parsed: its imports, keyword arguments and names in code type."""
    import ast
    import importlib

    import earful
    import earful.livekit as adapter

    front, _, body = SKILL.removeprefix("---\n").partition("\n---\n")
    assert SKILL.startswith("---\nname: earful\n") and "\ndescription: " in "\n" + front
    # The section for Pipecat is held to the Pipecat adapter by tests/test_pipecat.py.
    before, _, rest = body.partition("\n## A Pipecat bot\n")
    body = before + "\n## " + rest.partition("\n## ")[2]

    blocks = re.findall(r"```python\n(.*?)```", body, flags=re.DOTALL)
    spans = re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", body, flags=re.DOTALL))
    trees = [ast.parse(block) for block in blocks]
    for span in spans:
        try:
            trees.append(ast.parse(span))
        except SyntaxError:
            continue  # prose in code type: a command, a rule's name, a fragment

    known = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("earful"):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{node.module} has no {alias.name}"
                    known[alias.name] = getattr(module, alias.name)
    assert {"Case", "Effect", "ScriptedModel", "UserSpeech", "effect", "run", "explore"} <= set(
        known
    )

    methods = {"when": adapter.ScriptedModel.when, "otherwise": adapter.ScriptedModel.otherwise}
    checked = 0
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in known:
                target = known[node.func.id]
            elif isinstance(node.func, ast.Attribute) and node.func.attr in methods:
                target = methods[node.func.attr]
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "assert_contracts":
                target = adapter.RunResult.assert_contracts
            else:
                continue
            takes = inspect.signature(target).parameters
            for keyword in node.keywords:
                assert keyword.arg in takes, f"{ast.unparse(node.func)} takes no {keyword.arg}"
                checked += 1
    assert checked >= 10

    elsewhere = {"pytest", "earful", "AgentSession"}
    for span in spans:
        if re.fullmatch(r"[A-Za-z_]+", span) and span not in elsewhere:
            assert hasattr(adapter, span) or hasattr(earful, span), f"the package has no {span}"

    commands = _commands()
    named = set(re.findall(r"`earful ([a-z]+) ", body))
    assert named and named <= set(commands), named - set(commands)
    assert "skills/earful/SKILL.md" in README


def test_the_signatures_on_the_api_page_are_those_of_the_code():
    import earful
    import earful.livekit as adapter
    from earful.pytest_plugin import Earful

    kinds = inspect.Parameter
    checked = set()
    for holder, stub in _stubs():
        if holder is None:
            target = getattr(adapter, stub.name, None) or getattr(earful, stub.name)
        else:
            target = getattr(Earful if holder == "Earful" else getattr(adapter, holder), stub.name)
        named = [*stub.args.posonlyargs, *stub.args.args]
        defaults = [None] * (len(named) - len(stub.args.defaults)) + stub.args.defaults
        said = [(a.arg, kinds.POSITIONAL_OR_KEYWORD, d) for a, d in zip(named, defaults)]
        if stub.args.vararg:
            said.append((stub.args.vararg.arg, kinds.VAR_POSITIONAL, None))
        said += [
            (a.arg, kinds.KEYWORD_ONLY, d)
            for a, d in zip(stub.args.kwonlyargs, stub.args.kw_defaults)
        ]
        if stub.args.kwarg:
            said.append((stub.args.kwarg.arg, kinds.VAR_KEYWORD, None))
        real = list(inspect.signature(target).parameters.values())
        where = f"{holder + '.' if holder else ''}{stub.name}"
        assert [(name, kind) for name, kind, _ in said] == [(p.name, p.kind) for p in real], where
        for (name, _, default), parameter in zip(said, real):
            if default is None:
                assert parameter.default is kinds.empty, f"{where}: {name} has a default"
            else:
                assert ast.literal_eval(default) == parameter.default, f"{where}: {name}"
        checked.add(where)
    functions = {"run", "run_case", "explore", "cross_check", "keep", "replay", "reply", "effect"}
    assert functions | {"check", "ScriptedModel.when", "RunResult.assert_contracts"} <= checked
    assert {f"Earful.{name}" for name in ("run", "explore", "keep", "replay")} <= checked


def _documented():
    text = (ROOT / "docs" / "compatibility.md").read_text()
    text = text.split("## The options Earful models on LiveKit")[1].split("\n## ")[0]
    session, agent = text.split("`Agent` options.")

    def rows(table):
        return {
            line.split(" | ")[0]: line.split(" | ")[1]
            for line in table.splitlines()
            if line.startswith(("| `", "| Any"))
        }

    return rows(session), rows(agent)


def _names(cells):
    return {name for cell in cells for name in re.findall(r"`([a-z_]+)`", cell)}


def test_the_compatibility_page_lists_the_options_the_code_does():
    from earful.livekit import options

    session, agent = _documented()
    modelled = options.PASSED.keys() | options.FROM_CASE.keys() | options.REFUSED.keys()
    assert _names(session) == modelled
    passed = [cell for cell, does in session.items() if does == "Passed as they are"]
    assert _names(passed) == options.PASSED.keys()
    refused = [cell for cell, does in session.items() if does.startswith("Refused")]
    assert _names(refused) <= options.REFUSED.keys()
    named = options.AGENT_RUN | options.AGENT_TURNS | options.AGENT_REFUSED.keys()
    keys_of_turn_handling = {"enabled", "min_duration", "false_interruption_timeout"}
    assert named <= _names(agent) <= named | keys_of_turn_handling


def test_the_page_on_scripting_the_model_runs_as_written():
    """Each block builds its model, and each model answers the conversation it is shown for."""
    from earful.livekit import ScriptedModel

    page = (ROOT / "docs" / "scripting.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", page, flags=re.DOTALL)
    assert len(blocks) == 3
    built = []
    for block in blocks:
        names: dict = {}
        exec(compile(block, "scripting.md", "exec"), names)  # noqa: S102
        assert isinstance(names["model"], ScriptedModel)
        built.append(names)

    def said(model, *items):
        return model(list(items))

    user = {"type": "message", "role": "user", "text": "A table for four tonight, please."}
    failed = {"type": "function_call_output", "call_id": "c1", "name": "create_reservation"}
    first, retries, cancels = (names["model"] for names in built)
    assert said(first, user).tool_calls[0].name == "create_reservation"
    error = {**failed, "output": "timed out", "is_error": True}
    assert said(retries, user, error).tool_calls  # it tries once more
    again = {**error, "call_id": "c2"}
    assert not said(retries, user, error, again).tool_calls  # and then gives up
    cancel = {"type": "message", "role": "user", "text": "Actually, cancel that."}
    assert (
        said(cancels, user, cancel).text == "Understood."
        and not said(cancels, user, cancel).tool_calls
    )
    assert built[2]["speeches"][1].retires == ("book-table",)
