"""The portable core must never import livekit."""
import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "earful" / "core"


def test_core_does_not_import_livekit():
    offenders = []
    for path in CORE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.level >= 2:  # "from ..livekit import x"
                    names.append("livekit")
            if any(n == "livekit" or n.startswith("livekit.") for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"core imports livekit: {offenders}"
