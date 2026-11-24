"""The core and the shared scripted world must never import a framework or an adapter, an
adapter never imports the other, and a test file says which framework it needs."""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
PORTABLE = (SRC / "earful" / "core", SRC / "earful" / "scripted")
KERNEL = SRC / "earful" / "scripted" / "kernel"
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


def test_the_kernel_imports_no_concrete_party():
    implementations = tuple(
        f"earful.scripted.{name}" for name in ("model", "parties", "playbook", "voice", "world")
    )
    sources = sorted(KERNEL.rglob("*.py"))
    assert KERNEL / "ports.py" in sources
    offenders = [
        f"{path.relative_to(SRC)}:{line} imports {name}"
        for path in sources
        for line, name in _imports(path)
        if any(name == impl or name.startswith(impl + ".") for impl in implementations)
    ]
    assert not offenders, offenders


def test_the_kernel_addresses_parties_without_naming_a_provider_kind():
    """Catch a named port such as voice_quiet or a lookup such as parties['stt']."""
    roles = {"stt", "vad", "llm", "model", "tts", "voice"}
    offenders = []
    for path in KERNEL.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            for field, name in ast.iter_fields(node):
                if (
                    field in {"id", "arg", "attr", "name", "value"}
                    and isinstance(name, str)
                    and name.isidentifier()
                    and roles.intersection(name.lower().split("_"))
                ):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} names {name}")
    assert not offenders, offenders


@pytest.mark.parametrize(("adapter", "other"), [("livekit", "pipecat"), ("pipecat", "livekit")])
def test_an_adapter_imports_neither_the_other_adapter_nor_its_framework(adapter, other):
    """So a change to one adapter cannot change what the other does (scripts/ci_scope.py)."""
    sources = sorted((SRC / "earful" / adapter).rglob("*.py"))
    assert len(sources) > 5, adapter
    offenders = [
        f"{path.relative_to(SRC)}:{line} imports {name}"
        for path in sources
        for line, name in _imports(path)
        if any(name == f or name.startswith(f + ".") for f in (other, f"earful.{other}"))
    ]
    assert not offenders, offenders


def _own_private_members(tree):
    """Private methods and instance fields introduced in Earful's own classes."""
    members = set()
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for stmt in cls.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.add(stmt.name)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                members.add(stmt.target.id)
            elif isinstance(stmt, ast.Assign):
                members.update(target.id for target in stmt.targets if isinstance(target, ast.Name))
        members.update(
            node.attr
            for node in ast.walk(cls)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )
    return members


@pytest.mark.parametrize("adapter", ["livekit", "pipecat"])
def test_framework_private_access_and_attribute_defaults_live_in_seams(adapter):
    folder = SRC / "earful" / adapter
    offenders = []
    for path in sorted(folder.rglob("*.py")):
        if path == folder / "seams.py":
            continue
        tree = ast.parse(path.read_text())
        owned = _own_private_members(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if (
                    node.attr.startswith("_")
                    and not node.attr.startswith("__")
                    and node.attr not in owned
                ):
                    offenders.append((path.name, node.lineno, node.attr))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "setattr", "hasattr"}
                and len(node.args) >= 2
            ):
                name = node.args[1]
                private = (
                    isinstance(name, ast.Constant)
                    and isinstance(name.value, str)
                    and name.value.startswith("_")
                    and not name.value.startswith("__")
                )
                if private or (node.func.id == "getattr" and len(node.args) == 3):
                    offenders.append((path.name, node.lineno, ast.unparse(node)))
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


def _import_error(adapter: str, setup: str) -> str:
    """What importing ``adapter`` raises in a fresh process after ``setup``, as its repr."""
    code = f"import sys\n{setup}\ntry:\n    import {adapter}\nexcept ImportError as exc:\n"
    code += "    print(repr(exc))\n"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.mark.parametrize(
    ("adapter", "framework", "package", "extra"),
    [
        ("earful.livekit", "livekit.agents", "livekit-agents", "livekit"),
        ("earful.pipecat", "pipecat", "pipecat-ai", "pipecat"),
    ],
)
def test_an_adapter_names_its_framework_as_missing_only_when_it_is(
    adapter, framework, package, extra, tmp_path
):
    parts = framework.split(".")
    assert _import_error(adapter, f"sys.modules[{parts[0]!r}] = None") == repr(
        ImportError(
            f"{adapter} needs {package}, which is not installed. "
            f'Install it with: pip install "earful[{extra}]"'
        )
    )
    # A framework that is installed and fails to import one of its own dependencies.
    for depth in range(1, len(parts) + 1):
        tmp_path.joinpath(*parts[:depth]).mkdir()
        tmp_path.joinpath(*parts[:depth], "__init__.py").touch()
    tmp_path.joinpath(*parts, "__init__.py").write_text("import a_module_nobody_has\n")
    assert _import_error(adapter, f"sys.path.insert(0, {str(tmp_path)!r})") == repr(
        ModuleNotFoundError("No module named 'a_module_nobody_has'")
    )


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
