"""The pages agree with the LiveKit adapter: its settings, its names and the searches they quote."""

import inspect
import pickle
import pydoc
import re
import subprocess
import sys

import pytest

pytest.importorskip("livekit.agents")

from test_docs import README, REPORT_DOC, ROOT, SEARCH_MENTIONED, TESTING_DOC, _commands

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
        "data: 'dict[str, Any] | None' = None, seed: 'int' = 0) -> 'RunResult'"
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
    (planned,) = re.findall(
        r"^(earful: (\d+) cases .* to find them)\. The budget", done.stderr, re.MULTILINE
    )
    line, cases = planned
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
