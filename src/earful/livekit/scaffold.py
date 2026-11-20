"""``earful init``: a first test for an agent, written from the agent's own function tools."""

from __future__ import annotations

import importlib
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any

import jsonschema
import livekit.agents as lk
import pydantic
from livekit.agents import llm as lk_llm

_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_TOOLS = (lk_llm.FunctionTool, lk_llm.RawFunctionTool)


@dataclass(frozen=True)
class Scaffold:
    """A test file for one agent: the name of the file and its text."""

    filename: str
    source: str
    tools: tuple[str, ...]  # the agent's function tools, each declared an effect
    unbuilt: str | None = None  # why the agent could not be built to read its tools


def _snake(name: str) -> str:
    # SupportAgent -> support_agent
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return spaced.lower()


def _literal(value: Any) -> str:
    # Strings in double quotes, as the rest of the written file has them.
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, tuple):
        return "(" + ", ".join(_literal(v) for v in value) + ")"
    return repr(value)


def _laid_out(before: str, value: Any, after: str = "", indent: int = 0) -> list[str]:
    """``before``, the literal and ``after`` on one line, or one item a line past 100 columns."""
    pad = " " * indent
    whole = f"{pad}{before}{_literal(value)}{after}"
    if len(whole) <= 100 or not isinstance(value, (dict, list, tuple)) or not value:
        return [whole]
    opening, closing = {dict: ("{", "}"), list: ("[", "]"), tuple: ("(", ")")}[type(value)]
    items = value.items() if isinstance(value, dict) else ((None, item) for item in value)
    lines = [f"{pad}{before}{opening}"]
    for key, item in items:
        lines += _laid_out("" if key is None else f"{_literal(key)}: ", item, ",", indent + 4)
    return [*lines, f"{pad}{closing}{after}"]


def _resolve(reference: str) -> tuple[str, str, Any]:
    if not _REFERENCE.match(reference):
        raise ValueError(
            f"an agent is named as module:Class, such as myapp.agent:SupportAgent; "
            f"got {reference!r}"
        )
    module, _, name = reference.partition(":")
    try:
        target = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"no agent {reference!r}: {exc}") from exc
    if not (inspect.isclass(target) and issubclass(target, lk.Agent)):
        raise ValueError(
            f"{reference} is not a class of agent (a subclass of livekit.agents.Agent). "
            "`earful init` writes a test for a livekit-agents agent only"
        )
    return module, name, target


def _needs(agent: type[lk.Agent]) -> list[inspect.Parameter]:
    # The arguments the agent cannot be constructed without.
    wanted = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    parameters = list(inspect.signature(agent.__init__).parameters.values())[1:]  # not self
    return [
        p
        for p in parameters
        if p.default is inspect.Parameter.empty
        and p.kind in (*wanted, inspect.Parameter.KEYWORD_ONLY)
    ]


def _given(needs: list[inspect.Parameter]) -> tuple[list[Any], dict[str, Any]]:
    positional = [None for p in needs if p.kind is inspect.Parameter.POSITIONAL_ONLY]
    named = {p.name: None for p in needs if p.kind is not inspect.Parameter.POSITIONAL_ONLY}
    return positional, named


def _tools(agent: type[lk.Agent], needs: list[inspect.Parameter]) -> tuple[list[Any], str | None]:
    """The agent's tools, read from one built as the test will build it, and why if it cannot be."""
    build: Any = agent
    positional, named = _given(needs)
    unbuilt = None
    try:
        found = build(*positional, **named).tools
    except Exception as exc:
        if not needs:
            raise ValueError(
                f"{agent.__name__}() raised {type(exc).__name__}: {exc}. "
                "`earful init` builds the agent to read its tools"
            ) from exc
        unbuilt = f"{type(exc).__name__}: {exc}"
        found = lk_llm.find_function_tools(object.__new__(agent))  # the tools its class declares
    tools = sorted((t for t in found if isinstance(t, _TOOLS)), key=lambda t: t.info.name)
    return tools, unbuilt


_FORMATS = {
    "date-time": "2026-01-01T12:00:00Z",
    "date": "2026-01-01",
    "time": "12:00:00",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com",
}
_PLAIN = {"integer": 1, "number": 1, "boolean": True, "null": None}


def _placeholder(schema: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> Any:
    """A value of the simplest kind ``schema`` allows. ``_arguments`` asks the tool if it fits."""
    if depth > 16:  # a model that holds itself
        return None
    if "$ref" in schema:
        return _placeholder(defs.get(schema["$ref"].rpartition("/")[2], {}), defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for options in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(options, []):
            if option.get("type") != "null":
                return _placeholder(option, defs, depth + 1)
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        return {
            name: _placeholder(properties.get(name, {}), defs, depth + 1)
            for name in schema.get("required", [])
        }
    if kind == "array":
        item = _placeholder(schema.get("items", {}), defs, depth + 1)
        return [item] * schema.get("minItems", 0)
    if kind == "string":
        return _FORMATS.get(schema.get("format", ""), "example")
    return _PLAIN.get(kind if isinstance(kind, str) else "", "example")


def _arguments(tool: Any) -> dict[str, Any]:
    """A value for each argument ``tool`` requires, or ``ValueError`` if the tool refuses them."""
    name = tool.info.name
    try:
        if isinstance(tool, lk_llm.RawFunctionTool):
            parameters = tool.info.raw_schema.get("parameters", {})
        else:
            schema = lk_llm.utils.build_legacy_openai_schema(tool, internally_tagged=True)
            parameters = schema["parameters"]
        arguments: dict[str, Any] = _placeholder(
            {**parameters, "type": "object"}, parameters.get("$defs", {})
        )
        if isinstance(tool, lk_llm.RawFunctionTool):
            jsonschema.validate(arguments, parameters)
        else:
            lk_llm.utils.function_arguments_to_pydantic_model(tool).model_validate(arguments)
    except (pydantic.ValidationError, jsonschema.ValidationError) as exc:
        if isinstance(exc, pydantic.ValidationError):
            where, why = exc.errors()[0]["loc"], exc.errors()[0]["msg"]
        else:
            where, why = tuple(exc.absolute_path), exc.message
        raise ValueError(
            f"`earful init` cannot write a call of the tool {name!r}: it has no value for the "
            f"argument {'.'.join(str(part) for part in where)!r} that the tool accepts ({why})"
        ) from exc
    except Exception as exc:
        raise ValueError(
            f"`earful init` cannot write a call of the tool {name!r}: it could not read the "
            f"tool's arguments ({type(exc).__name__}: {' '.join(str(exc).split())[:200]})"
        ) from exc
    return arguments


def _call(tools: list[Any]) -> tuple[str, dict[str, Any]]:
    """The first tool a call can be written for, and its arguments."""
    refused = []
    for tool in tools:
        try:
            return tool.info.name, _arguments(tool)
        except ValueError as exc:
            refused.append(exc)
    raise refused[0]


def scaffold(reference: str) -> Scaffold:
    """The first test of the agent ``reference`` names, as ``module:Class``. Raises
    ``ValueError`` for a reference that does not name a class of agent."""
    module, name, agent = _resolve(reference)
    needs = _needs(agent)
    tools, unbuilt = _tools(agent, needs)
    names = [tool.info.name for tool in tools]
    called, arguments = _call(tools) if tools else ("", {})
    scenario = _snake(name)
    uses = ["Case", "Effect", "ScriptedModel", "UserSpeech"] if tools else None
    lines = [
        f'"""A first Earful test of {name}, written by `earful init`. Edit what is marked."""',
        "",
        f"from earful.livekit import {', '.join(uses or ['Case', 'ScriptedModel', 'UserSpeech'])}",
    ]
    if tools:
        lines += ["from earful.livekit import tool_failed, tool_succeeded"]
    lines += ["", f"from {module} import {name}", ""]

    if tools:
        intent = _literal(called.replace("_", "-"))
        lines += [
            "# The scripted model: the first rule that holds replies. It calls the tool again",
            "# once if the call fails, as a model does. Change the tool, its arguments and the",
            "# words to what your agent does.",
            *_laid_out("CALL = ", (called, arguments)),
            "model = (",
            "    ScriptedModel()",
            f'    .when(tool_succeeded(), say="That is done.", claims=[{intent}])',
            "    .when(",
            "        tool_failed() & (lambda context: len(context.tool_results) >= 2),",
            '        say="Sorry, I could not do that.",',
            "    )",
            '    .when(tool_failed(), say="One moment, let me try that again.", call=CALL)',
            '    .otherwise(say="One moment.", call=CALL)',
            ")",
        ]
    else:
        lines += [
            "# The scripted model. Add `.when(user_said(...), say=...)` rules before `otherwise`.",
            'model = ScriptedModel().otherwise(say="How can I help?")',
        ]
    lines += ["", ""]

    if needs:
        given = ", ".join(
            "None" if p.kind is inspect.Parameter.POSITIONAL_ONLY else f"{p.name}=None"
            for p in needs
        )
        works = (
            "    # None works while every tool is an effect below. Give a stand-in for what a"
            if unbuilt is None
            else "    # None does not build it: give a stand-in for each, and for what a"
        )
        lines += [
            f"def build() -> {name}:",
            f"    # {name} is built with: {', '.join(p.name for p in needs)}.",
            works,
            "    # tool calls when you take that tool out of `effects`.",
            f"    return {name}({given})",
            "",
            "",
        ]

    lines += [
        f"def {scenario}() -> Case:",
        "    return Case(",
        f"        name={_literal(scenario)},",
        f"        agent={'build' if needs else name},",
    ]
    if tools:
        lines += [
            "        # Each tool here is one durable write: in a run its body does not run.",
            "        # Delete the line of a tool that only reads, and hand the agent a stand-in.",
            "        # No write has an idempotency key, so the call the model makes again after a",
            "        # timeout writes twice, and the search fails on A2. If the tool sends a key",
            "        # its service honours, declare both: Effect(..., key=...) here, and",
            '        # backend_mode="keyed" on this Case.',
            "        effects={",
            *(
                f"            {_literal(tool)}: Effect({_literal(tool.replace('_', '-'))}),"
                for tool in names
            ),
            "        },",
        ]
    lines += [
        "        # What the user says, in seconds from the start.",
        "        speeches=[",
        '            UserSpeech(start=0.5, end=2.5, transcript="Please help me with this."),',
        "        ],",
        "        policy=model,",
        "    )",
        "",
        "",
        "# A contract named here must reach a verdict: a test that judged nothing fails.",
        f"REQUIRED = {_literal(['A2', 'A3', 'L1'] if tools else ['L1'])}",
        "",
        "",
        "def test_the_conversation_as_written(earful):",
        f"    earful.run({scenario}).assert_contracts(require=REQUIRED)",
        "",
        "",
        "def test_every_timing_and_fault_near_it(earful):",
        f"    earful.explore({scenario}).assert_contracts(require=REQUIRED)",
    ]
    return Scaffold(f"test_{scenario}.py", "\n".join(lines) + "\n", tuple(names), unbuilt)


__all__ = ["Scaffold", "scaffold"]
