"""The portable core must never import livekit, directly or through Earful's adapter."""
import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
CORE = SRC / "earful" / "core"
FORBIDDEN = ("livekit", "earful.livekit")


def _module(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path):
    """Every module ``path`` imports, as an absolute name."""
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


def test_core_does_not_import_livekit():
    offenders = [
        f"{path.relative_to(SRC)}:{line} imports {name}"
        for path in sorted(CORE.rglob("*.py"))
        for line, name in _imports(path)
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    ]
    assert not offenders, offenders


def test_core_imports_with_livekit_unavailable():
    """Import every core module in a fresh interpreter in which livekit cannot be imported."""
    modules = sorted(_module(p) for p in CORE.rglob("*.py"))
    assert "earful.core.checker" in modules
    code = (
        "import sys, importlib\n"
        "sys.modules['livekit'] = None\n"  # any 'import livekit' now raises ImportError
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "assert not any(m == 'earful.livekit' or m.startswith('earful.livekit.') for m in sys.modules)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
