"""Read executable blocks with their source locations so failures point back to the page."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def blocks(page: str, language: str) -> list[str]:
    text = (ROOT / page).read_text()
    return re.findall(rf"^```{language}\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)


def execute(page: str, block: str, names: dict[str, Any]) -> None:
    text = (ROOT / page).read_text()
    line = text[: text.index(block)].count("\n")
    exec(compile("\n" * line + block, page, "exec"), names)  # noqa: S102


def commands(page: str) -> list[list[str]]:
    return [
        args
        for block in blocks(page, "bash")
        for line in block.splitlines()
        if (args := shlex.split(line, comments=True))
    ]
