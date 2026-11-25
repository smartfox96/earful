"""What ``earful init`` writes, whatever the framework: placeholder values and the test's text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Scaffold:
    """A test file for one agent or bot: the name of the file and its text."""

    filename: str
    source: str
    tools: tuple[str, ...]  # tools or functions visible during construction
    noun: str = "tools"  # what the framework calls them


def select_writes(writes: tuple[str, ...], names: list[str]) -> list[str]:
    missing = sorted(set(writes) - set(names))
    if missing:
        raise ValueError(f"unknown declared write(s) {missing}; visible tools: {names}")
    return list(dict.fromkeys(writes))


def snake(name: str) -> str:
    # SupportAgent -> support_agent
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return spaced.lower()


def literal(value: Any) -> str:
    # Strings in double quotes, as the rest of the written file has them.
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    if isinstance(value, tuple):
        return "(" + ", ".join(literal(v) for v in value) + ")"
    return repr(value)


def laid_out(before: str, value: Any, after: str = "", indent: int = 0) -> list[str]:
    """``before``, the literal and ``after`` on one line, or one item a line past 100 columns."""
    pad = " " * indent
    whole = f"{pad}{before}{literal(value)}{after}"
    if len(whole) <= 100 or not isinstance(value, (dict, list, tuple)) or not value:
        return [whole]
    opening, closing = {dict: ("{", "}"), list: ("[", "]"), tuple: ("(", ")")}[type(value)]
    items = value.items() if isinstance(value, dict) else ((None, item) for item in value)
    lines = [f"{pad}{before}{opening}"]
    for key, item in items:
        lines += laid_out("" if key is None else f"{literal(key)}: ", item, ",", indent + 4)
    return [*lines, f"{pad}{closing}{after}"]


_FORMATS = {
    "date-time": "2026-01-01T12:00:00Z",
    "date": "2026-01-01",
    "time": "12:00:00",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com",
}
_PLAIN = {"integer": 1, "number": 1, "boolean": True, "null": None}


def placeholder(schema: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> Any:
    """A value of the simplest kind ``schema`` allows. The caller asks the tool if it fits."""
    if depth > 16:  # a model that holds itself
        return None
    if "$ref" in schema:
        return placeholder(defs.get(schema["$ref"].rpartition("/")[2], {}), defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for options in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(options, []):
            if option.get("type") != "null":
                return placeholder(option, defs, depth + 1)
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        return {
            name: placeholder(properties.get(name, {}), defs, depth + 1)
            for name in schema.get("required", [])
        }
    if kind == "array":
        item = placeholder(schema.get("items", {}), defs, depth + 1)
        return [item] * schema.get("minItems", 0)
    if kind == "string":
        return _FORMATS.get(schema.get("format", ""), "example")
    return _PLAIN.get(kind if isinstance(kind, str) else "", "example")


def model_lines(
    called: str, arguments: dict[str, Any], *, noun: str = "tool", owner: str = "your agent"
) -> list[str]:
    """The scripted model of a first test: it calls ``called``, and once more if that fails."""
    intent = literal(called.replace("_", "-"))
    return [
        f"# The scripted model: the first rule that holds replies. It calls the {noun} again",
        f"# once if the call fails, as a model does. Change the {noun}, its arguments and the",
        f"# words to what {owner} does.",
        *laid_out("CALL = ", (called, arguments)),
        "model = (",
        "    ScriptedModel()",
        f'    .when(tool_succeeded(), say="That is done.", claims=[{intent}])',
        "    .when(",
        "        tool_failed() & (lambda context: len(context.tool_results) >= 2),",
        '        say="Sorry, I could not do that.",',
        "    )",
        '    .when(tool_failed(), say="One moment, let me try that again.", call=CALL)',
        '    .when(user_said("Please help me with this."), say="One moment.", call=CALL)',
        '    .otherwise(say="How can I help?")',
        ")",
    ]


def tests_lines(scenario: str, required: list[str]) -> list[str]:
    """The two tests of a first test file: the run as written, and the search around it."""
    return [
        "# A contract named here must reach a verdict: a test that judged nothing fails.",
        f"REQUIRED = {literal(required)}",
        "",
        "",
        "def test_the_conversation_as_written(earful):",
        f"    earful.run({scenario}).assert_contracts(require=REQUIRED)",
        "",
        "",
        "def test_every_timing_and_fault_near_it(earful):",
        f"    earful.explore({scenario}).assert_contracts(require=REQUIRED)",
    ]


def effects_lines(names: list[str], *, noun: str = "tool", owner: str = "the agent") -> list[str]:
    """The ``effects=`` of a first test: each tool or function is one durable write."""
    return [
        f"        # Each {noun} here is one durable write: in a run its body does not run.",
        "        # These writes were named with --write when the test was generated.",
        "        # No write has an idempotency key, so the call the model makes again after a",
        f"        # timeout writes twice, and the search fails on A2. If the {noun} sends a key",
        "        # its service honours, declare both: Effect(..., key=...) here, and",
        '        # backend_mode="keyed" on this Case.',
        "        effects={",
        *(
            f"            {literal(name)}: Effect({literal(name.replace('_', '-'))}),"
            for name in names
        ),
        "        },",
    ]


__all__ = [
    "Scaffold",
    "effects_lines",
    "laid_out",
    "literal",
    "model_lines",
    "placeholder",
    "snake",
    "tests_lines",
]
