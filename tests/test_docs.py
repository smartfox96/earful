"""The documentation agrees with the code, schemas and workflows it describes."""

import ast
import importlib
import json
import re
import sys
from pathlib import Path

from earful.core.schema import kinds, load_schema
from earful.scripted.releases import SUPPORT

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ci_scope  # the cells CI runs

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
        row = next(line for line in DOC.splitlines() if line.startswith(f"| `{kind}` |"))
        direct = set(defs[kind].get("properties", {}))
        missing += [f"{kind}: {name}" for name in direct if f"`{name}`" not in row]
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


TESTING_DOC = (Path(__file__).resolve().parents[1] / "docs" / "testing.md").read_text()
REPORT_DOC = (Path(__file__).resolve().parents[1] / "docs" / "report.md").read_text()


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
    from earful.core.checker.report import MISSING, RULES, lost_names, rule_names

    named = set(re.findall(r"`([AML]\d/[a-z0-9 -]+)`", TESTING_DOC))
    assert set(rule_names()) <= named and named - set(rule_names()) <= set(lost_names())
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
        for code in MISSING[contract]:  # each code a verdict of it can carry is in its table
            assert f"`{code}`" in sections[contract], f"{contract}/{code}"


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
            **{name: {} for name in re.findall(r"`(\w+)` \(default `\{\}`", row)},
        }
        for contract, row in rows.items()
    }
    assert stated == {
        contract: {
            f.name: getattr(cls(), f.name)
            for f in dataclasses.fields(cls)
            if not isinstance(f.default, tuple)
        }
        for contract, cls in POLICIES.items()
    }


COMPATIBILITY = (ROOT / "docs" / "compatibility.md").read_text()
FULL_TIER = sorted(ci_scope.plan("full", None).cells)
_PINNED = r"^\| (\d+\.\d+\.\d+) \| (\d{4}-\d\d-\d\d) \|"


def _section(heading):
    """The part of the compatibility page under ``## heading``."""
    _, found, rest = COMPATIBILITY.partition(f"\n## {heading}\n")
    assert found, heading
    return rest.partition("\n## ")[0]


def test_release_metadata_and_ci_match_the_declared_releases():
    with (ROOT / "releases.toml").open("rb") as source:
        toml = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")
        declared = toml.load(source)
    assert declared.keys() == SUPPORT.keys()
    for package, matrix in (("livekit-agents", ci_scope.LIVEKIT), ("pipecat-ai", ci_scope.PIPECAT)):
        support = SUPPORT[package]
        releases = declared[package]
        assert list(releases["tested"].items()) == list(matrix.items())
        assert tuple(matrix) == support.tested
        assert (releases["lowest"], releases["below"]) == (support.lowest, support.below)
    series = dict.fromkeys(v.rsplit(".", 1)[0] for v in declared["pipecat-ai"]["tested"])
    sentence = f"Supported pipecat-ai series: {', '.join(series)}."
    for page in ("CONTRIBUTING.md", "docs/limits.md", "docs/compatibility.md"):
        assert sentence in (ROOT / page).read_text(), page


def test_the_supported_releases_are_the_same_everywhere_they_are_listed():
    section = _section("livekit-agents")
    declared = dict(re.findall(_PINNED, section, re.MULTILINE))
    assert tuple(declared) == SUPPORT["livekit-agents"].tested
    assert ci_scope.LIVEKIT == declared
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
    pinned = dict(re.findall(_PINNED, section, re.MULTILINE))
    assert tuple(pinned) == SUPPORT["pipecat-ai"].tested
    locks = sorted((ROOT / "compat").glob("pc-*.lock.txt"))
    assert [p.name for p in locks] == [f"pc-{release}.lock.txt" for release in pinned]
    for release, path in zip(pinned, locks):
        assert f"`compat/{path.name}`" in section
        assert f"pipecat-ai=={release}\n" in path.read_text()
    release, as_of = next(reversed(pinned.items()))
    script = (ROOT / "scripts" / "make_pipecat_env.sh").read_text()
    assert f'version="${{1:-{release}}}"\nas_of="${{2:-{as_of}}}"\n' in script
    assert f"pipecat-ai {release} as of {as_of}, in .venvs/pc-{release}" in script
    # The lint environment resolves as of the later of the two newest releases' dates.
    newest_livekit = max(ci_scope.LIVEKIT.values())
    lint_env = (ROOT / "scripts" / "make_lint_env.sh").read_text()
    assert f"--exclude-newer {max(as_of, newest_livekit)} \\\n" in lint_env
    assert f'"pipecat-ai=={release}"' in lint_env


def test_a_lock_file_is_the_list_the_environment_scripts_write():
    locks = sorted((ROOT / "compat").glob("*.lock.txt"))
    assert len(locks) == sum(len(s.tested) for s in SUPPORT.values())
    for path in locks:
        lines = path.read_text().splitlines()
        assert all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*==[^\s=]+", line) for line in lines), path
        names = [line.partition("==")[0] for line in lines]
        assert names == sorted(names) and "earful" not in names, path


def test_each_adapter_is_installed_by_its_extra_on_the_pages_a_reader_starts_from():
    project = (ROOT / "pyproject.toml").read_text()
    readme, pipecat = (ROOT / "README.md").read_text(), (ROOT / "docs" / "pipecat.md").read_text()
    for extra, pages in (("livekit", [readme]), ("pipecat", [readme, pipecat])):
        assert f"\n{extra} = [" in project, extra
        assert all(f'pip install "earful[{extra}]"' in page for page in pages), extra
    source = (ROOT / "src" / "earful" / "pipecat" / "__init__.py").read_text()
    assert 'pip install "earful[pipecat]"' in source  # what the adapter says when it is missing


def test_each_extra_accepts_the_releases_the_page_and_the_adapter_say_it_does():
    project = (ROOT / "pyproject.toml").read_text()
    livekit, pipecat = SUPPORT["livekit-agents"], SUPPORT["pipecat-ai"]
    assert f'livekit = ["livekit-agents{livekit.requirement}"]' in project
    said = f"from {livekit.lowest} up to, and not\nincluding, {livekit.below}"
    assert said in _section("livekit-agents")
    # The Pipecat extra takes the minor series of the tested release, where pipecat-ai installs.
    major, minor = (int(part) for part in pipecat.tested[0].split(".")[:2])
    newest_minor = int(pipecat.tested[-1].split(".")[1])
    assert (pipecat.lowest, pipecat.below) == (f"{major}.{minor}", f"{major}.{newest_minor + 1}")
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
    pinned = dict(re.findall(_PINNED, _section("pipecat-ai"), re.MULTILINE))
    assert ci_scope.PIPECAT == pinned
    cells = sorted(
        (c.version, c.python, c.system, c.source) for c in FULL_TIER if c.framework == "pc"
    )
    systems = {"linux": "Linux", "macos": "macOS"}
    sources = {"checkout": "the checkout", "wheel": "the built wheel"}
    table = re.findall(
        r"^\| ([\d.]+) \| ([\d.]+) \| (Linux|macOS) \| (the checkout|the built wheel) \|$",
        _section("What CI runs for Pipecat"),
        re.MULTILINE,
    )
    assert table == [(v, py, systems[os], sources[source]) for v, py, os, source in cells]
    # Every Python from the extra's marker to the newest, both systems, and the wheel once.
    pythons = sorted({py for _, py, _, _ in cells})
    assert pythons == ["3.11", "3.12", "3.13"]
    assert {os for _, _, os, _ in cells} == set(systems)
    assert [source for _, _, _, source in cells].count("wheel") == len(pinned)
    supported = (
        f"pipecat-ai {', '.join(pinned)}, on Linux and macOS, Python {pythons[0]} to {pythons[-1]}."
    )
    assert supported in README


def test_the_ci_table_on_the_compatibility_page_is_what_the_full_tier_runs():
    table = re.findall(
        r"^\| ([\d.]+) \| ([A-Za-z, ]+) \| ([\d., ]*)\|$",
        _section("What CI runs for LiveKit"),
        re.MULTILINE,
    )
    systems = {"linux": "Linux", "macos": "macOS"}
    expected = []
    for release in ci_scope.LIVEKIT:
        cells = [c for c in FULL_TIER if c.framework == "lk" and c.version == release]
        on = ", ".join(systems[c.system] for c in cells if c.python == "3.13")
        others = ", ".join(c.python for c in cells if c.python != "3.13")
        expected.append((release, on, f"{others} " if others else ""))
    assert table == expected
    # Python 3.10 to 3.14, each with one release.
    pythons = sorted({c.python for c in FULL_TIER if c.framework == "lk"}, key=lambda p: int(p[2:]))
    assert pythons == ["3.10", "3.11", "3.12", "3.13", "3.14"]
    assert "Python 3.10 to 3.14 are each tested with one release." in COMPATIBILITY


def test_the_page_names_the_oldest_jsonschema_the_package_accepts():
    project = (ROOT / "pyproject.toml").read_text()
    (floor,) = re.findall(r'^dependencies = \["jsonschema>=([\d.]+)"\]$', project, re.MULTILINE)
    assert f"the oldest jsonschema the package accepts, {floor}.0 " in " ".join(
        _section("What CI runs for LiveKit").split()
    )


README = (ROOT / "README.md").read_text()


def test_the_releases_with_each_known_defect_are_the_same_everywhere_they_are_listed():
    page = (ROOT / "docs" / "compatibility.md").read_text()
    rows = re.findall(
        r"^\| (\d+\.\d+\.\d+) \| [\d-]+ \| `[^`]+` \| (present|repaired) \| (present|repaired) \|",
        page,
        re.MULTILINE,
    )
    assert len(rows) == len(SUPPORT["livekit-agents"].tested)
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


def test_each_case_page_quotes_the_hash_its_bundle_records():
    for page in sorted((ROOT / "docs" / "cases").glob("*.md")):
        text = page.read_text()
        (bundle,) = re.findall(r"Bundle: `(tests/bundles/[^`]+)/`", text)
        manifest = json.loads((ROOT / bundle / "manifest.json").read_text())
        (quoted,) = re.findall(r"Recorded ledger hash on [\d.]+:\s+`([0-9a-f]{64})`", text)
        assert quoted == manifest["minimal"]["hash"], page.name


def test_the_readme_shows_the_example_as_it_is_in_the_repository():
    for name in ("agent.py", "test_booking.py"):
        source = (ROOT / "examples" / "booking" / name).read_text()
        assert f"```python\n{source}```" in README, f"the README does not show {name} as it is"
    assert 'idempotency_key=f"table-for-{party_size}"' in README
    for line in (
        "A2/rule  in 272 case(s)",
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


API = (ROOT / "docs" / "api.md").read_text()
TROUBLESHOOTING = (ROOT / "docs" / "troubleshooting.md").read_text()
LIMITS = (ROOT / "docs" / "limits.md").read_text()
GLOSSARY = (ROOT / "docs" / "glossary.md").read_text()
SOURCE = ROOT / "src" / "earful"
_PACKAGES = {"earful": "", "earful.livekit": "livekit", "earful.pipecat": "pipecat"}


def _exported(package):
    # Read from the source: an adapter imports only where its framework is installed.
    tree = ast.parse((SOURCE / _PACKAGES[package] / "__init__.py").read_text())
    (names,) = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "__all__"
    ]
    return set(names)


def _sections(page, level="###"):
    """Each heading of ``level`` on a Markdown page, with the text under it."""
    found = re.findall(
        rf"^{level} ([^\n]+)\n(.*?)(?=^#{{2,3}} |\Z)", page, re.MULTILINE | re.DOTALL
    )
    assert len(dict(found)) == len(found), "two sections have one heading"
    return dict(found)


def _rows(text):
    """Each row of the tables in ``text``, as its first cell and the cells after it."""
    rows = re.findall(r"^\|(.*)\|$", text, re.MULTILINE)
    cells = [[cell.strip() for cell in row.split("|")] for row in rows]
    return [(row[0], row[1:]) for row in cells if row[0] != "---"]


def test_the_api_page_lists_the_public_names_of_each_package():
    names = API.partition("\n## Names\n")[2]
    table = _rows(names.partition("\n## ")[0])
    assert table[0] == ("Name", [f"`{package}`" for package in _PACKAGES])
    assert all(mark in ("yes", "") for _, marks in table[1:] for mark in marks)
    for column, package in enumerate(_PACKAGES):
        listed = {name.strip("`") for name, marks in table[1:] if marks[column] == "yes"}
        assert listed == _exported(package), listed ^ _exported(package)
    body = names.partition("\n## ")[2]
    undescribed = [
        name for name, _ in table[1:] if name not in body and f"def {name[1:-1]}(" not in body
    ]
    assert not undescribed, f"docs/api.md lists and does not describe {undescribed}"
    # The names said to be each adapter's own are those both import from their own modules.
    (apart,) = re.findall(
        r"Nine names are\nthe exception: (.*?) are each adapter's own", names, re.DOTALL
    )
    own = []
    for adapter in ("livekit", "pipecat"):
        tree = ast.parse((SOURCE / adapter / "__init__.py").read_text())
        own.append(
            {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level == 1
                for alias in node.names
            }
        )
    # Each adapter's api module hands on the one WorkersUnavailable.
    assert set(re.findall(r"`(\w+)`", apart)) == (own[0] & own[1]) - {"WorkersUnavailable"}


def _stubs():
    """Each function the API page gives a signature for, with the class it is a method of."""
    found = []
    for block in re.findall(r"```python\n(.*?)```", API, flags=re.DOTALL):
        for node in ast.parse(block).body:
            holders = [(None, node)] if isinstance(node, ast.FunctionDef) else []
            if isinstance(node, ast.ClassDef):
                holders = [(node.name, method) for method in node.body]
            for holder, function in holders:
                assert ast.unparse(function.body[0]) == "...", f"{function.name} is no signature"
                found.append((holder, function))
    return found


def _class(name, adapter=None):
    """A class's fields with their defaults, and every name its body defines, from its source."""
    found = [
        node
        for path in sorted((SOURCE / adapter if adapter else SOURCE).rglob("*.py"))
        # The checker's view of a ledger has an Effect of its own.
        if adapter or (path.parent.name not in ("livekit", "pipecat") and path.name != "records.py")
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    if not found and adapter:
        return _class(name)
    assert len(found) == 1, f"{len(found)} classes are named {name}"
    fields, members = {}, set()
    shared = {
        "ScriptedCase": "Case",
        "RunEvidence": "RunEvidence",
        "DrainOptions": "DrainOptions",
        "Application": "Application",
    }
    for base in reversed(found[0].bases):
        if isinstance(base, ast.Name) and base.id in shared:
            inherited, names = _class(shared[base.id], "scripted")
        elif (
            adapter
            and isinstance(base, ast.Name)
            and base.id in {"AgentFactory", "BotFactory", "SessionOptions", "PipelineOptions"}
        ):
            inherited, names = _class(base.id, adapter)
        else:
            continue
        fields.update(inherited)
        members.update(names)
    for node in found[0].body:
        if isinstance(node, ast.AnnAssign):
            fields[ast.unparse(node.target)] = node.value and ast.unparse(node.value)
            members.add(ast.unparse(node.target))
        elif isinstance(node, ast.FunctionDef) and node.name != "__init__":
            members.add(node.name)
    return fields, members


_FACTORIES = {"field(default_factory=list)": "[]", "field(default_factory=dict)": "{}"}
_TABLE_OF = re.compile(r"`(\w+)`(?: for (LiveKit|Pipecat))?$")
_ADAPTER_PARTS = {"ClockedSink": "livekit", "Ends": "pipecat"}


def _described(heading):
    name, adapter = _TABLE_OF.search(heading).groups()
    return name, (adapter or "").lower() or _ADAPTER_PARTS.get(name)


def test_each_table_of_fields_on_the_api_page_is_the_class_as_the_code_defines_it():
    tables = 0
    for heading, text in _sections(API).items():
        rows = _rows(text)
        if not rows or rows[0][0] != "Field":
            continue
        fields, _ = _class(*_described(heading))
        said = {name.strip("`"): cells[0].strip("`") for name, cells in rows[1:]}
        assert list(said) == list(fields), heading
        for name, default in fields.items():
            default = "required" if default is None else _FACTORIES.get(default, default)
            assert said[name] == default, f"{heading}: {name}"
        tables += 1
    assert tables == 10


def test_each_table_of_attributes_on_the_api_page_names_what_the_class_has():
    tables = 0
    for heading, text in _sections(API).items():
        rows = _rows(text)
        if not rows or rows[0][0] != "Attribute":
            continue
        name, adapter = _described(heading)
        named = {attribute.strip("`"): cells for attribute, cells in rows[1:]}
        if rows[0][1][0] == "Adapter":
            (_, livekit), (_, pipecat) = _class(name, "livekit"), _class(name, "pipecat")
            assert {a for a, cells in named.items() if cells[0] == "both"} == livekit & pipecat
            assert {a for a, cells in named.items() if cells[0] == "LiveKit"} == livekit - pipecat
            assert not pipecat - livekit
        else:
            _, members = _class(name, adapter)
            assert set(named) <= members, f"{heading}: {set(named) - members}"
        tables += 1
    assert tables == 8


def test_the_api_page_lists_every_command_with_exactly_its_options():
    from earful import cli

    commands = _commands()
    sections = {
        heading[len("`earful ") : -1]: text
        for heading, text in _sections(API).items()
        if heading.startswith("`earful ")
    }
    assert list(sections) == list(commands)
    for name, text in sections.items():
        options = set(commands[name]._option_string_actions) - {"-h", "--help"}
        said = {option for first, _ in _rows(text) for option in re.findall(r"`(-[a-z-]+)", first)}
        assert said == options, f"earful {name}: {said ^ options}"
        for action in commands[name]._get_positional_actions():
            assert f"`earful {name} {action.metavar.split('|')[0]}" in text, f"earful {name}"
    statuses = _rows(API.partition("\n| Exit status |")[2].partition("\n\n")[0])
    assert [int(status) for status, _ in statuses] == sorted(
        [cli.OK, cli.BROKEN, cli.REFUSED, cli.NO_VERDICT]
    )
    plugin = (SOURCE / "pytest_plugin.py").read_text()
    page = API.partition("\n## The pytest plugin\n")[2]
    assert set(re.findall(r"`(--earful-[a-z-]+)", page)) == set(
        re.findall(r'"(--earful-[a-z-]+)"', plugin)
    )


def test_every_name_the_api_and_troubleshooting_pages_import_exists():
    import importlib

    imported = []
    for block in re.findall(r"```python\n(.*?)```", API + TROUBLESHOOTING, flags=re.DOTALL):
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("earful"):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{node.module} has no {alias.name}"
                    imported.append(alias.name)
    assert {"check", "read_ledger", "UntestedRelease"} <= set(imported)


def _parts(node):
    """The literal text of a string expression in order, with None wherever a value goes."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [part for value in node.values for part in _parts(value)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_parts(node.left), *_parts(node.right)]
    return [None]


def _messages():
    """A pattern for the end of each message in the source with 12 characters of its own."""
    patterns = set()
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                continue
            parts = [part and re.sub(r"\s+", " ", part) for part in _parts(node)]
            if sum(len(part.strip()) for part in parts if part is not None) >= 12:
                pattern = "".join(".*?" if part is None else re.escape(part) for part in parts)
                patterns.add(re.sub(" {2,}", " ", pattern.replace(r"\ ", " ")))
    return [re.compile(pattern + " ?$") for pattern in patterns]


def test_every_message_the_troubleshooting_page_quotes_is_one_the_code_gives():
    quoted = re.findall(r"```text\n(.*?)```", TROUBLESHOOTING, flags=re.DOTALL)
    assert len(quoted) >= 50
    messages = _messages()
    checked = 0
    for block in quoted:
        lines = [line for line in block.splitlines() if line]
        # An indented line under the first is what a message lists: a count, an example.
        for line in [lines[0], *(line for line in lines[1:] if not line.startswith(" "))]:
            text = " " + " ".join(line.split()) + " "
            assert any(message.search(text) for message in messages), f"no message ends:\n{line}"
            checked += 1
    assert checked > len(quoted)
    # The two that list releases list the releases supported now.
    livekit = SUPPORT["livekit-agents"]
    assert f"The tested releases are: {', '.join(livekit.tested)}\n" in TROUBLESHOOTING
    said = f"from {livekit.lowest} up to, and not including, {livekit.below}, and"
    assert said in TROUBLESHOOTING
    quoted_on = (
        f"livekit-agents\n{livekit.tested[-1]} or pipecat-ai {SUPPORT['pipecat-ai'].tested[0]}"
    )
    assert quoted_on in TROUBLESHOOTING


def test_the_troubleshooting_page_says_what_exercises_each_contract_as_the_checker_does():
    from earful.core.checker.contracts import DECLARED

    section = _sections(TROUBLESHOOTING)["Not exercised"]
    exercised_by = {name: contract.exercised_by for name, contract in DECLARED.items()}
    assert {contract: cells[0] for contract, cells in _rows(section)[1:]} == exercised_by


def _links(text):
    """Each link in ``text`` that is not to a site, as its path and its anchor."""
    return re.findall(r"\]\((?!https?://)([^)#\s]*)(?:#([^)\s]+))?\)", text)


def test_the_readme_lists_every_page_of_the_documentation():
    readme = (ROOT / "README.md").read_text()
    listed = set(re.findall(r"\| \[`docs/([a-z_]+\.md)`\]", readme))
    assert listed == {page.name for page in (ROOT / "docs").glob("*.md")}


def test_every_link_between_the_pages_leads_to_a_heading_that_exists():
    pages = sorted((ROOT / "docs").glob("*.md"))
    assert {"api.md", "glossary.md", "limits.md", "troubleshooting.md"} <= {p.name for p in pages}
    followed = 0
    for page in pages:
        for path, anchor in _links(page.read_text()):
            target = (page.parent / path).resolve() if path else page
            assert target.is_file(), f"{page.name} links to {path}"
            if anchor:
                assert anchor in _anchors(target.read_text()), f"{page.name}: {path}#{anchor}"
            followed += 1
    assert followed >= 100
    for name in ("api", "troubleshooting", "limits", "glossary"):
        assert f"https://github.com/smartfox96/earful/blob/main/docs/{name}.md" in README
        assert f"]({name}.md)" in TESTING_DOC


def test_every_term_of_the_glossary_is_one_the_page_it_points_to_uses():
    in_code = "".join(
        path.read_text()
        for path in (
            SOURCE / "cli.py",
            SOURCE / "core/bundle.py",
            *(SOURCE / "core/search").glob("*.py"),
        )
    )
    terms = []
    for heading, text in _sections(GLOSSARY, "##").items():
        rows = _rows(text)
        assert rows[0] == ("Term", ["Meaning", "Explained in"]), heading
        for term, (meaning, explained) in rows[1:]:
            ((path, _),) = _links(explained)
            term = term.strip("`")
            used = (ROOT / "docs" / path).read_text().lower()
            assert term.lower() in used or term in in_code, f"{path} does not use {term!r}"
            assert meaning and not meaning.endswith("."), term
            terms.append(term)
    assert len(terms) == len(set(terms)) >= 60
    assert {"case", "variable", "window", "reading", "bundle", "replay", "witness"} <= set(terms)


def test_the_limits_page_quotes_the_numbers_the_code_has():
    from earful.core.checker.report import RULES
    from earful.core.checker.view import MAX_READINGS
    from earful.core.search import DELAY_LIMIT_US, OFFSETS_US
    from earful.scripted.ceilings import SPIN_PASSES, STALL_SECONDS

    page = " ".join(LIMITS.split())
    assert f"After {SPIN_PASSES:,} loop passes".replace(",", " ") in " ".join(COMPATIBILITY.split())
    assert f"| livekit-agents | {', '.join(SUPPORT['livekit-agents'].tested)} |" in LIMITS
    assert f"| pipecat-ai | {', '.join(SUPPORT['pipecat-ai'].tested)} |" in LIMITS
    assert f"more than {MAX_READINGS} readings" in page
    for text in (page, " ".join(TROUBLESHOOTING.split())):
        assert f"ended after {STALL_SECONDS:g} real seconds" in text
    step = max(OFFSETS_US) // 1000
    assert sorted(OFFSETS_US) == [-step * 1000, 0, step * 1000]
    assert f"put {step} ms before a transition, at it and {step} ms after it" in page
    assert f"No delay is made longer than {DELAY_LIMIT_US // 1_000_000} s" in page
    project = (ROOT / "pyproject.toml").read_text()
    pythons = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', project)
    assert f"| Python | {pythons[0]} to {pythons[-1]} with livekit-agents" in LIMITS
    # The row of each contract links to that contract's section.
    rows = _rows(_sections(LIMITS, "##")["What each contract cannot decide"])
    linked = [re.match(r"\[(\w\d)\]\(contracts\.md#(\w\d)-", name).groups() for name, _ in rows[1:]]
    assert linked == [(contract, contract.lower()) for contract in RULES]


def _assigned(path, name):
    """Each value ``name`` is given in the module at ``path``, at any depth."""
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]


def _keyword(call, name):
    return next((k.value for k in getattr(call, "keywords", ()) if k.arg == name), None)


def _first_cells(table):
    return [name.replace("`", "").lower() for name, _ in _rows(table)[1:]]


def test_the_limits_page_lists_each_defect_the_tests_declare_and_no_other():
    import conformance
    import invariants

    tests = ROOT / "tests"
    known = _sections(LIMITS)
    varied = set()
    for framework, heading in (("livekit", "LiveKit"), ("pipecat", "Pipecat")):
        shapes = sorted((tests / "shapes" / framework).glob("[a-z]*.py"))
        declared = {path.stem for path in shapes if _assigned(path, "DEFECT")}
        link = rf"\]\(\.\./tests/shapes/{framework}/(\w+)\.py\)"
        listed = re.findall(link, known[f"{heading} applications"])
        assert sorted(listed) == sorted(declared), f"{heading} applications"
        for path in shapes:
            for value in _assigned(path, "DEFECT_VARIED"):
                varied |= {getattr(invariants, key.id) for key in value.keys}
    changes = _first_cells(known["A scenario changed in a way that should not matter"])
    assert sorted(changes) == sorted(varied)

    behaviours = {b.name.lower(): b for b in conformance.BEHAVIOURS}
    rows = _rows(known["What every adapter should do"])
    assert _first_cells(known["What every adapter should do"]) == list(behaviours)
    for column, framework in enumerate(("livekit", "pipecat")):
        (scenarios,) = _assigned(tests / f"test_{framework}_conformance.py", "SCENARIOS")
        wrong = {
            getattr(conformance, key.attr).name.lower()
            for key, scenario in zip(scenarios.keys, scenarios.values)
            if _keyword(scenario, "defect") is not None
        }
        said = {
            name.replace("`", "").lower() for name, cells in rows[1:] if cells[column] != "Holds"
        }
        assert said == wrong, framework

    reasons = set()
    for path in sorted(tests.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            raises, reason = _keyword(node, "raises"), _keyword(node, "reason")
            if isinstance(raises, ast.Name) and raises.id == "KnownDefect" and reason is not None:
                reasons.add(ast.literal_eval(reason).removeprefix("Earful: ").lower())
    assert sorted(_first_cells(known["Elsewhere"])) == sorted(reasons)


def test_the_framework_independent_examples_run_on_a_recorded_ledger(tmp_path, monkeypatch):
    from doc_examples import blocks, execute

    from earful import read_ledger
    from earful.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").symlink_to(ROOT / "tests", target_is_directory=True)
    ledger = next((ROOT / "tests/golden").glob("*.book_then_interrupt.*"))
    for filename in ("run.ledger.jsonl", "booking.ledger.jsonl"):
        (tmp_path / filename).symlink_to(ledger)
    names = {"events": read_ledger(ledger)}
    for page in ("docs/report.md", "docs/contracts.md", "docs/api.md", "docs/troubleshooting.md"):
        for block in blocks(page, "python"):
            if page == "docs/api.md" and block.startswith(("class ", "def ")):
                continue  # public signatures are compared with inspect.signature
            execute(page, block, names)
    from doc_examples import commands

    for command, *arguments in commands("docs/report.md"):
        assert command == "earful" and main(arguments) == 0
        assert (tmp_path / "report.html").stat().st_size > 0


def test_command_blocks_use_the_installed_cli_and_declared_environments():
    from doc_examples import commands

    from earful.cli import _parser

    parser = _parser()
    pages = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"), *(ROOT / "skills").rglob("*.md")]
    for page in pages:
        for program, *args in commands(str(page)):
            if Path(program).name == "earful":
                parser.parse_args(args)
                if program.startswith(".venvs/lk-"):
                    assert program.split("/")[1][3:] in SUPPORT["livekit-agents"].tested
            elif program == "scripts/make_env.sh":
                assert ci_scope.LIVEKIT[args[0]] == args[1]
            elif program == "pip":
                assert args in (["install", "earful[livekit]"], ["install", "earful[pipecat]"])
            else:
                assert program in {"pytest", "git", "cd", "open"} or program.endswith(
                    "/bin/python"
                ), (page, program)


def test_the_unobservable_example_is_a_complete_line_from_the_report():
    from earful import check, read_ledger
    from earful.core.ledger import run_id

    ledger = next((ROOT / "tests/golden").glob("*.book_then_interrupt.*"))
    events = [e for e in read_ledger(ledger) if not e["kind"].startswith("effect.")]
    run = run_id(events[0]["data"])
    report = check([{**event, "seq": n, "run": run} for n, event in enumerate(events)])
    section = _sections(TROUBLESHOOTING)["Unobservable"]
    (quoted,) = re.findall(r"```text\n(.*?)\n```", section, flags=re.DOTALL)
    assert quoted in [line.strip() for line in report.lines()]
