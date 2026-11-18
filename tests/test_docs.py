"""docs/ledger.md and the JSON Schema describe the same ledger."""

import re
from pathlib import Path

from earful.core.schema import kinds, load_schema

DOC = (Path(__file__).resolve().parents[1] / "docs" / "ledger.md").read_text()
MENTIONED = set(re.findall(r"`([^`\s]+)`", DOC))
_KIND = re.compile(r"^(run|history|user|tts|tool|llm|sink|playout|effect)\.[a-z_]+$")


def _field_names(schema, defs):
    """Every property name in ``schema``, following references."""
    names = set()
    if "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[1]]
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key, []):
            names |= _field_names(option, defs)
    for key in ("items", "then", "else", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            names |= _field_names(schema[key], defs)
    for name, sub in schema.get("properties", {}).items():
        names.add(name)
        if isinstance(sub, dict):
            names |= _field_names(sub, defs)
    return names


def test_every_kind_and_field_in_the_schema_is_documented():
    defs = load_schema()["$defs"]
    missing = [kind for kind in kinds() if kind not in MENTIONED]
    for kind in kinds():
        missing += [
            f"{kind}: {name}" for name in sorted(_field_names(defs[kind], defs) - MENTIONED)
        ]
    assert not missing, f"docs/ledger.md does not mention {missing}"


def test_every_kind_the_document_names_is_in_the_schema():
    # The names of the delays a scenario can declare look like kinds and are not.
    delays = set(
        load_schema()["$defs"]["scenario"]["properties"]["delays"]["items"]["properties"]["of"][
            "enum"
        ]
    )
    documented = {token for token in MENTIONED if _KIND.match(token)} - delays
    assert documented and documented <= set(kinds()), documented - set(kinds())


def test_the_envelope_is_documented():
    assert set(load_schema()["required"]) <= MENTIONED


SEARCH_DOC = (Path(__file__).resolve().parents[1] / "docs" / "search.md").read_text()
SEARCH_MENTIONED = set(re.findall(r"`([^`\s]+)`", SEARCH_DOC))


def test_every_field_of_a_case_and_of_a_bundle_manifest_is_documented():
    from earful.core import bundle, case

    missing = {}
    for name, schema in (
        ("earful.case/1", case.load_schema()),
        ("earful.bundle/1", bundle.load_schema()),
    ):
        fields = _field_names(schema, schema.get("$defs", {}))
        if fields - SEARCH_MENTIONED:
            missing[name] = sorted(fields - SEARCH_MENTIONED)
    assert not missing, f"docs/search.md does not mention {missing}"


def test_every_setting_the_livekit_adapter_has_is_documented():
    from earful.livekit.scenario import SETTINGS

    assert set(SETTINGS) <= SEARCH_MENTIONED, sorted(set(SETTINGS) - SEARCH_MENTIONED)


TESTING_DOC = (Path(__file__).resolve().parents[1] / "docs" / "testing.md").read_text()
REPORT_DOC = (Path(__file__).resolve().parents[1] / "docs" / "report.md").read_text()


def test_every_name_the_testing_page_imports_from_earful_exists():
    """The examples on the testing and report pages are read as Python, and each name
    they import from the package is looked up."""
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


def test_every_command_and_option_the_testing_page_names_exists():
    from earful.cli import _parser

    parser = _parser()
    commands = parser._subparsers._group_actions[0].choices
    named = set(re.findall(r"`earful ([a-z]+)[ `]", TESTING_DOC))
    assert named == set(commands), named ^ set(commands)
    known = {option for sub in commands.values() for option in sub._option_string_actions}
    plugin = {"--earful-out", "--earful-workers"}
    options = set(re.findall(r"`(--[a-z-]+)", TESTING_DOC)) - plugin
    assert options and options <= known, options - known


ROOT = Path(__file__).resolve().parents[1]


def test_the_supported_releases_are_the_same_everywhere_they_are_listed():
    """The compatibility page, the CI matrix, the full check and the lock files name the
    same releases, each with the same date."""
    page = (ROOT / "docs" / "compatibility.md").read_text()
    declared = dict(re.findall(r"^\| (\d+\.\d+\.\d+) \| (\d{4}-\d\d-\d\d) \|", page, re.MULTILINE))
    assert len(declared) == 5
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    in_ci = dict(re.findall(r'version: "([\d.]+)", as_of: "([\d-]+)"', workflow))
    assert in_ci == declared
    script = (ROOT / "scripts" / "check_all.sh").read_text()
    assert dict(re.findall(r'"([\d.]+) (\d{4}-\d\d-\d\d)"', script)) == declared
    locked = {path.name[3:-9] for path in (ROOT / "compat").glob("lk-*.lock.txt")}
    assert locked == set(declared)
    for release in declared:
        assert f"`compat/lk-{release}.lock.txt`" in page
        assert (
            f"livekit-agents=={release}\n" in (ROOT / f"compat/lk-{release}.lock.txt").read_text()
        )
    # The reference ledgers exist for exactly these releases.
    golden = {path.name.split(".book")[0][3:] for path in (ROOT / "tests/golden").glob("*.book_*")}
    assert golden == set(declared)


def test_the_extra_accepts_the_releases_the_page_says_it_does():
    page = (ROOT / "docs" / "compatibility.md").read_text()
    project = (ROOT / "pyproject.toml").read_text()
    assert 'livekit = ["livekit-agents>=1.5.6,<1.9"]' in project
    assert "from 1.5.6 up to, and not\nincluding, 1.9" in page


def test_the_ci_table_on_the_compatibility_page_is_what_the_workflow_runs():
    page = (ROOT / "docs" / "compatibility.md").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    table = {
        release: (macos, pythons.strip())
        for release, macos, pythons in re.findall(
            r"^\| ([\d.]+) \| every change \| (every change|nightly) \| ([\d., ]*)\|",
            page,
            re.MULTILINE,
        )
    }
    assert len(table) == 5
    # macOS: the releases the workflow leaves out of a pull request run nightly only.
    left_out = set(re.findall(r"\|\| 'macos-latest' }}\n\s+lk: \{ version: \"([\d.]+)\"", workflow))
    assert {release for release, (macos, _) in table.items() if macos == "nightly"} == left_out
    # Every other Python, once each, with the release the workflow pairs it with.
    extra = re.findall(r'python: "([\d.]+)"\n\s+lk: \{ version: "([\d.]+)"', workflow)
    assert extra
    for release, (_, pythons) in table.items():
        expected = sorted(python for python, with_release in extra if with_release == release)
        assert sorted(filter(None, (p.strip() for p in pythons.split(",")))) == expected


README = (ROOT / "README.md").read_text()


def test_the_readme_shows_the_example_as_it_is_in_the_repository():
    """The quickstart's two files are the files of examples/booking, which a test runs
    as shipped and with the fix, and scripts/quickstart_check.sh runs from the wheel."""
    for name in ("agent.py", "test_booking.py"):
        source = (ROOT / "examples" / "booking" / name).read_text()
        assert f"```python\n{source}```" in README, f"the README does not show {name} as it is"
    # The fix it gives is the fix the test of the example applies.
    assert 'idempotency_key=f"table-for-{party_size}"' in README
    # The failure it quotes is the failure the test of the example expects.
    for line in (
        "A2/rule  in 261 case(s)",
        "2 durable effects for 'book-table'; the scenario authorized 1",
        "changed: backend.timeout 1, commit first",
        "search 1 (booking) breaks A2/rule: backend.timeout 1, commit first",
    ):
        assert line in README, line


SKILL = (ROOT / "skills" / "earful" / "SKILL.md").read_text()


def test_the_skill_for_coding_agents_uses_only_what_exists():
    """skills/earful/SKILL.md is what a coding assistant reads in place of these pages,
    so a name it gets wrong is a name every assistant gets wrong. Its code is read as
    Python: each name imported from the package is looked up, each keyword given to
    something of the package is one that it takes, and each name the text sets in code
    type is one the package has."""
    import ast
    import importlib
    import inspect

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

    # A bare name in code type is the package's, or one of the few that are not.
    elsewhere = {"pytest", "earful", "AgentSession"}
    for span in spans:
        if re.fullmatch(r"[A-Za-z_]+", span) and span not in elsewhere:
            assert hasattr(adapter, span) or hasattr(earful, span), f"the package has no {span}"

    commands = _commands()
    named = set(re.findall(r"`earful ([a-z]+) ", body))
    assert named and named <= set(commands), named - set(commands)
    assert "skills/earful/SKILL.md" in README


def _commands():
    from earful.cli import _parser

    return _parser()._subparsers._group_actions[0].choices


def test_every_page_and_command_the_readme_names_exists():
    from earful.cli import _parser

    # Every link is absolute, so that it also works on the package index page. Each
    # points at a file or a directory of this repository, at a picture in it, or at a
    # page of the site that scripts/site.sh builds and the pages workflow publishes.
    targets = re.findall(r"\]\(([^)#]+)\)", README)
    assert targets
    site = (ROOT / "scripts" / "site.sh").read_text()
    published = set(re.findall(r'"\$out/([a-z0-9-]+\.html)"', site))
    assert {"index.html", "booking.html"} <= published
    kinds = set()
    for target in targets:
        page = re.fullmatch(r"https://smartfox96\.github\.io/earful/(.+)", target)
        picture = re.fullmatch(
            r"https://raw\.githubusercontent\.com/smartfox96/earful/main/(.+\.png)", target
        )
        found = re.fullmatch(r"https://github\.com/smartfox96/earful/(blob|tree)/main/(.+)", target)
        if page:
            assert page.group(1) in published, f"scripts/site.sh builds no {page.group(1)}"
        elif picture:
            assert (ROOT / picture.group(1)).is_file(), target
        else:
            assert found, f"{target} is not a link into the repository or its site"
            path = ROOT / found.group(2)
            assert path.is_dir() if found.group(1) == "tree" else path.is_file(), target
        kinds.add("page" if page else "picture" if picture else "file")
    assert kinds == {"page", "picture", "file"}
    commands = _parser()._subparsers._group_actions[0].choices
    named = set(re.findall(r"^earful ([a-z]+) ", README, re.MULTILINE))
    assert named and named <= set(commands), named - set(commands)
    page = (ROOT / "docs" / "compatibility.md").read_text()
    releases = re.findall(r"^\| (\d+\.\d+\.\d+) \| \d{4}", page, re.MULTILINE)
    assert f"livekit-agents {', '.join(releases[:-1])} and {releases[-1]}" in README
