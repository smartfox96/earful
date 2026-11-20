"""The documentation agrees with the code, schemas and workflows it describes."""

import re
from pathlib import Path

from earful.core.schema import kinds, load_schema
from earful.scripted.releases import SUPPORT

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


def test_the_rules_a_test_can_allow_are_listed_and_each_is_a_row_of_its_contract():
    from earful.core.checker.report import RULES, rule_names

    assert set(re.findall(r"`([AML]\d/[a-z0-9 -]+)`", TESTING_DOC)) == set(rule_names())
    page = (ROOT / "docs" / "contracts.md").read_text()
    sections = dict(
        re.findall(r"^### ([AML]\d)\. .*?\n(.*?)(?=^##)", page, re.MULTILINE | re.DOTALL)
    )
    for contract, rules in RULES.items():
        rows = [
            row.lower() for row in re.findall(r"^\| ([^|]+) \|", sections[contract], re.MULTILINE)
        ]
        for rule in rules:
            assert any(row.startswith(rule) for row in rows), f"{contract}/{rule}"


def test_the_policy_defaults_are_those_the_contracts_page_states():
    import dataclasses

    from earful.core.checker.policy import POLICIES

    page = (ROOT / "docs" / "contracts.md").read_text()
    row = re.compile(r"^### ([AML]\d)\. .*?^\| Policy \| (.*?) \|$", re.MULTILINE | re.DOTALL)
    rows = dict(row.findall(page))
    stated = {
        contract: {
            **{
                name: {"true": True, "false": False}.get(value, value)
                for name, value in re.findall(r"`(\w+)` \(default (true|false)", row)
            },
            **{
                name: int(value.replace(" ", ""))
                for name, value in re.findall(r"`(\w+)` \(default ([\d ]+)\)", row)
            },
            **dict(re.findall(r"`(\w+)`: `(\w+)` \(default\)", row)),
        }
        for contract, row in rows.items()
    }
    assert stated == {
        contract: {
            f.name: f.default for f in dataclasses.fields(cls) if not isinstance(f.default, tuple)
        }
        for contract, cls in POLICIES.items()
    }


COMPATIBILITY = (ROOT / "docs" / "compatibility.md").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
_PINNED = r"^\| (\d+\.\d+\.\d+) \| (\d{4}-\d\d-\d\d) \|"


def _section(heading):
    """The part of the compatibility page under ``## heading``."""
    _, found, rest = COMPATIBILITY.partition(f"\n## {heading}\n")
    assert found, heading
    return rest.partition("\n## ")[0]


def _job(name):
    """The text of one job of the ci workflow."""
    (text,) = re.findall(rf"^  {name}:\n(.*?)(?=^  [a-z_]+:\n|\Z)", CI, re.MULTILINE | re.DOTALL)
    return text


def test_the_supported_releases_are_the_same_everywhere_they_are_listed():
    section = _section("livekit-agents")
    declared = dict(re.findall(_PINNED, section, re.MULTILINE))
    assert tuple(declared) == SUPPORT["livekit-agents"].tested
    in_ci = dict(re.findall(r'version: "([\d.]+)", as_of: "([\d-]+)"', _job("determinism")))
    assert in_ci == declared
    script = (ROOT / "scripts" / "check_all.sh").read_text()
    assert dict(re.findall(r'"([\d.]+) (\d{4}-\d\d-\d\d)"', script)) == declared
    locked = {path.name[3:-9] for path in (ROOT / "compat").glob("lk-*.lock.txt")}
    assert locked == set(declared)
    for release in declared:
        assert f"`compat/lk-{release}.lock.txt`" in section
        assert (
            f"livekit-agents=={release}\n" in (ROOT / f"compat/lk-{release}.lock.txt").read_text()
        )
    golden = {path.name.split(".book")[0][3:] for path in (ROOT / "tests/golden").glob("*.book_*")}
    assert golden == set(declared)
    lint_env = (ROOT / "scripts" / "make_lint_env.sh").read_text()
    assert f'"livekit-agents=={list(declared)[-1]}"' in lint_env


def test_the_compatibility_page_names_every_clock_read_a_run_takes_from_the_loop():
    from earful.scripted.clock import VIRTUAL_READS

    section = _section("The virtual clock")
    assert [name for name in VIRTUAL_READS if f"`time.{name}()`" not in section] == []


def test_the_supported_pipecat_release_is_the_same_everywhere_it_is_listed():
    section = _section("pipecat-ai")
    ((release, as_of),) = re.findall(_PINNED, section, re.MULTILINE)
    assert (release,) == SUPPORT["pipecat-ai"].tested
    assert f"`compat/pc-{release}.lock.txt`" in section
    locks = [path.name for path in (ROOT / "compat").glob("pc-*.lock.txt")]
    assert locks == [f"pc-{release}.lock.txt"]
    assert f"pipecat-ai=={release}\n" in (ROOT / "compat" / locks[0]).read_text()
    script = (ROOT / "scripts" / "make_pipecat_env.sh").read_text()
    assert f'version="${{1:-{release}}}"\nas_of="${{2:-{as_of}}}"\n' in script
    assert f"pipecat-ai {release} as of {as_of}, in .venvs/pc-{release}" in script
    lint_env = (ROOT / "scripts" / "make_lint_env.sh").read_text()
    assert f"--exclude-newer {as_of} \\\n" in lint_env and f'"pipecat-ai=={release}"' in lint_env


def test_a_lock_file_is_the_list_the_environment_scripts_write():
    locks = sorted((ROOT / "compat").glob("*.lock.txt"))
    assert len(locks) == 6
    for path in locks:
        lines = path.read_text().splitlines()
        assert all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*==[^\s=]+", line) for line in lines), path
        names = [line.partition("==")[0] for line in lines]
        assert names == sorted(names) and "earful" not in names, path


def test_each_extra_accepts_the_releases_the_page_and_the_adapter_say_it_does():
    project = (ROOT / "pyproject.toml").read_text()
    livekit, pipecat = SUPPORT["livekit-agents"], SUPPORT["pipecat-ai"]
    assert f'livekit = ["livekit-agents{livekit.requirement}"]' in project
    said = f"from {livekit.lowest} up to, and not\nincluding, {livekit.below}"
    assert said in _section("livekit-agents")
    # The Pipecat extra takes the minor series of the tested release, where pipecat-ai installs.
    major, minor = (int(part) for part in pipecat.tested[0].split(".")[:2])
    assert (pipecat.lowest, pipecat.below) == (f"{major}.{minor}", f"{major}.{minor + 1}")
    accepted = f"pipecat-ai{pipecat.requirement}"
    assert f"pipecat = [\"{accepted}; python_version >= '3.11'\"]" in project
    said = f"from {pipecat.lowest} up to, and not\nincluding, {pipecat.below}, on Python 3.11"
    assert said in _section("pipecat-ai")
    assert (
        'requires-python = ">=3.10"' in project and '"pipecat"' in project.split("classifiers")[0]
    )


def test_the_compatibility_page_says_what_an_adapter_does_with_the_installed_release():
    for heading in ("livekit-agents", "pipecat-ai"):
        section = " ".join(_section(heading).split())
        assert "warns once" in section and "refuses to import" in section


def test_the_pipecat_table_on_the_compatibility_page_is_what_the_workflow_runs():
    ((release, as_of),) = re.findall(_PINNED, _section("pipecat-ai"), re.MULTILINE)
    job = _job("pipecat")
    assert re.findall(r'version: "([\d.]+)", as_of: "([\d-]+)"', job) == [(release, as_of)]
    cells = re.findall(r'\{ os: (\w+)-latest, python: "([\d.]+)", from: (\w+) \}', job)
    systems = {"ubuntu": "Linux", "macos": "macOS"}
    sources = {"checkout": "the checkout", "wheel": "the built wheel"}
    table = re.findall(
        r"^\| ([\d.]+) \| ([\d.]+) \| (Linux|macOS) \| (the checkout|the built wheel) \|$",
        _section("What CI runs for Pipecat"),
        re.MULTILINE,
    )
    assert table == [
        (release, python, systems[os], sources[source]) for os, python, source in cells
    ]
    # Every Python from the extra's marker to the newest, both systems, and the wheel once.
    pythons = sorted({python for _, python, _ in cells})
    assert pythons == ["3.11", "3.12", "3.13"]
    assert {os for os, _, _ in cells} == set(systems)
    assert [source for _, _, source in cells].count("wheel") == 1
    supported = f"pipecat-ai {release}, on Linux and macOS, Python {pythons[0]} to {pythons[-1]}."
    assert supported in README


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
    # The releases the workflow leaves out of a pull request on macOS run nightly only.
    left_out = set(re.findall(r"\|\| 'macos-latest' }}\n\s+lk: \{ version: \"([\d.]+)\"", workflow))
    assert {release for release, (macos, _) in table.items() if macos == "nightly"} == left_out
    # Each other Python runs once, with the release the workflow pairs it with.
    extra = re.findall(r'python: "([\d.]+)"\n\s+lk: \{ version: "([\d.]+)"', workflow)
    assert extra
    for release, (_, pythons) in table.items():
        expected = sorted(python for python, with_release in extra if with_release == release)
        assert sorted(filter(None, (p.strip() for p in pythons.split(",")))) == expected


README = (ROOT / "README.md").read_text()


def test_the_releases_with_each_known_defect_are_the_same_everywhere_they_are_listed():
    page = (ROOT / "docs" / "compatibility.md").read_text()
    rows = re.findall(
        r"^\| (\d+\.\d+\.\d+) \| [\d-]+ \| `[^`]+` \| (present|repaired) \| (present|repaired) \|",
        page,
        re.MULTILINE,
    )
    assert len(rows) == 5
    tests = (ROOT / "tests" / "test_historical_cases.py").read_text()
    for column, issue in ((1, "3702"), (2, "5509")):
        script = (ROOT / "scripts" / f"spike_{issue}.py").read_text()
        (fixed_in,) = re.findall(r"^FIXED_IN = \((\d+), (\d+), (\d+)\)", script, re.MULTILINE)
        assert f"FIX_{issue} = ({', '.join(fixed_in)})" in tests
        first_repaired = tuple(int(part) for part in fixed_in)
        present = [row[0] for row in rows if row[column] == "present"]
        repaired = [row[0] for row in rows if row[column] == "repaired"]
        assert all(tuple(map(int, r.split("."))) < first_repaired for r in present)
        assert all(tuple(map(int, r.split("."))) >= first_repaired for r in repaired)
        (shown,) = re.findall(rf"^\| \[livekit/agents#{issue}\].*$", README, re.MULTILINE)
        assert shown.endswith(f"| {', '.join(present)} | {', '.join(repaired)} |")


def test_the_readme_shows_the_example_as_it_is_in_the_repository():
    for name in ("agent.py", "test_booking.py"):
        source = (ROOT / "examples" / "booking" / name).read_text()
        assert f"```python\n{source}```" in README, f"the README does not show {name} as it is"
    assert 'idempotency_key=f"table-for-{party_size}"' in README
    for line in (
        "A2/rule  in 261 case(s)",
        "2 durable effects for 'book-table'; the scenario authorized 1",
        "changed: backend.timeout 1, commit first",
        "search 1 (booking) breaks A2/rule: backend.timeout 1, commit first",
    ):
        assert line in README, line


def test_the_readme_says_before_the_quickstart_that_a_test_shows_only_what_its_script_models():
    before, _, quickstart = README.partition("\n## Quickstart\n")
    assert "a test shows a failure only in\nbehaviour its script models" in before
    # What the limit is explained with is in the example: its model tries a failed call again.
    assert "tries a failed tool call again" in before
    assert '.when(tool_failed(), say="One moment, let me try that again.", call=BOOK)' in quickstart


def test_the_changelog_has_a_section_for_the_version_under_one_for_what_is_unreleased():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    project = (ROOT / "pyproject.toml").read_text()
    (version,) = re.findall(r'^version = "(.+)"$', project, re.MULTILINE)
    headings = re.findall(r"^## \[(.+?)\](?: - (\d{4}-\d\d-\d\d))?$", changelog, re.MULTILINE)
    assert len(headings) == len(re.findall(r"^## ", changelog, re.MULTILINE))
    names = [name for name, _ in headings]
    assert names[:2] == ["Unreleased", version], "the newest section is not this version's"
    assert not headings[0][1] and all(date for _, date in headings[1:])
    released = [tuple(int(part) for part in name.split(".")) for name in names[1:]]
    assert released == sorted(released, reverse=True)
    for name in names:
        link = rf"^\[{re.escape(name)}\]: https://github\.com/smartfox96/earful/"
        assert re.search(link, changelog, re.MULTILINE), f"[{name}] links nowhere"
    assert "https://github.com/smartfox96/earful/blob/main/CHANGELOG.md" in README
    assert 'Changelog = "https://github.com/smartfox96/earful/blob/main/CHANGELOG.md"' in project


SKILL = (ROOT / "skills" / "earful" / "SKILL.md").read_text()


def test_the_skill_for_coding_agents_uses_only_what_exists():
    """The skill's code is parsed: its imports, keyword arguments and names in code type."""
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

    # Links are absolute so that they also work on the package index page.
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
    releases = list(dict(re.findall(_PINNED, _section("livekit-agents"), re.MULTILINE)))
    assert f"livekit-agents {', '.join(releases[:-1])} and {releases[-1]}" in README


_FOR_CONTRIBUTORS = (
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
)
_IN_THE_REPOSITORY = r"(?:scripts|tests|docs|src|compat|examples|skills|\.github)/[^`\s<>*]+"


def _anchors(page):
    """The anchor GitHub gives each heading of a Markdown page."""
    headings = re.findall(r"^#+ (.+)$", page, re.MULTILINE)
    return {re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-") for heading in headings}


def test_the_files_for_contributors_name_only_what_exists():
    commands = _commands()
    for name in _FOR_CONTRIBUTORS:
        text = (ROOT / name).read_text()
        # A path in backticks, up to its arguments. One with a placeholder in it is left out.
        for path in re.findall(rf"`({_IN_THE_REPOSITORY})[`\s]", text):
            assert (ROOT / path).exists(), f"{name} names {path}"
        for command, rest in re.findall(r"`earful ([a-z]+)([^`]*)`", text):
            assert command in commands, f"{name} names earful {command}"
            for option in re.findall(r"--[a-z-]+", rest):
                assert option in commands[command].format_help(), f"{name}: {command} {option}"
        linked = r"https://github\.com/smartfox96/earful/blob/main/([^\s)#]+)(?:#([a-z0-9-]+))?"
        for path, anchor in re.findall(linked, text):
            assert (ROOT / path).is_file(), f"{name} links to {path}"
            assert not anchor or anchor in _anchors((ROOT / path).read_text()), f"{name}: #{anchor}"
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    for said in re.findall(r'`?CONTRIBUTING\.md`?, "([^"]+)"', (ROOT / "SECURITY.md").read_text()):
        assert f"\n## {said}\n" in contributing, said
    template = (ROOT / ".github/pull_request_template.md").read_text()
    assert 'CONTRIBUTING.md, "The checks"' in template and "\n## The checks\n" in contributing
