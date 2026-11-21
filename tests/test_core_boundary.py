"""The core and the shared scripted world must never import a framework or an adapter,
and a test file says which framework it needs."""

import ast
import re
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
PORTABLE = (SRC / "earful" / "core", SRC / "earful" / "scripted")
FORBIDDEN = ("livekit", "pipecat", "earful.livekit", "earful.pipecat")


def _module(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _sources() -> list[Path]:
    return sorted(path for directory in PORTABLE for path in directory.rglob("*.py"))


def _imports(path: Path, package: list[str] | None = None):
    """Every module ``path`` imports, as an absolute name."""
    if package is None:
        package = _module(path).split(".")
        if path.name != "__init__.py":
            package = package[:-1]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            yield node.lineno, module
            for alias in node.names:  # "from .. import livekit"
                yield node.lineno, f"{module}.{alias.name}"


def test_the_core_and_the_scripted_world_import_no_framework():
    offenders = [
        f"{path.relative_to(SRC)}:{line} imports {name}"
        for path in _sources()
        for line, name in _imports(path)
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    ]
    assert not offenders, offenders


def test_they_import_with_every_framework_unavailable():
    modules = sorted(_module(p) for p in _sources())
    assert {"earful.core.checker", "earful.scripted.clock"} <= set(modules)
    code = (
        "import sys, importlib\n"
        "sys.modules['livekit'] = sys.modules['pipecat'] = None\n"  # importing either now raises
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "adapters = ('earful.livekit', 'earful.pipecat')\n"
        "adapter = [m for m in sys.modules if m.startswith(adapters)]\n"
        "assert not adapter, adapter\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def _frameworks(path: Path, seen: frozenset[Path] = frozenset()) -> set[str]:
    """The frameworks ``path`` imports, itself or through another file under ``tests/``."""
    found = set()
    for _line, name in _imports(path, []):
        top, _, rest = name.partition(".")
        if top in ("livekit", "pipecat"):
            found.add(top)
        elif top == "earful" and rest.split(".")[0] in ("livekit", "pipecat"):
            found.add(rest.split(".")[0])
        elif (helper := TESTS / f"{top}.py").exists() and helper not in seen:
            found |= _frameworks(helper, seen | {path})
    return found


def test_a_test_file_skips_itself_where_a_framework_it_imports_is_absent():
    """So every other test runs where one framework is installed (scripts/ran_every_test.sh)."""
    skips_itself = re.compile(r'^pytest\.importorskip\("(livekit|pipecat)[."]', re.MULTILINE)
    files = sorted(TESTS.glob("test_*.py"))
    said = {path.name: set(skips_itself.findall(path.read_text())) for path in files}
    imported = {path.name: _frameworks(path) for path in files}
    # This one imports its framework in another process only (the imported_on fixture).
    imported["test_livekit_releases.py"] |= {"livekit"}
    assert said == imported
    for framework in ("livekit", "pipecat"):
        assert sum(framework in needs for needs in said.values()) > 2, framework
    assert sum(not needs for needs in said.values()) > 10  # the tests every environment runs
