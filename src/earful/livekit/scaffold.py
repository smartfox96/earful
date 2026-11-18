"""A first test for an agent, written from the agent's own tools.

``earful init myapp.agent:SupportAgent`` reads the agent's function tools and writes a
test file that runs as it is: one conversation in which the scripted model calls one of
the tools, run once and then searched.

Every tool is declared an effect (``Case.effects``), so in a run no tool body is
executed and nothing reaches a real service. Which tools write and which only read is
something the agent's author knows and this module cannot: the file says so where the
tools are listed, and the author deletes the lines of the tools that only read.

Nothing here is a model of the conversation the agent is for. The words the user says and
the tool the model calls are placeholders, marked as such.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any

import livekit.agents as lk
from livekit.agents import llm as lk_llm

_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_TOOLS = (lk_llm.FunctionTool, lk_llm.RawFunctionTool)


@dataclass(frozen=True)
class Scaffold:
    """A test file for one agent: the name of the file and its text."""

    filename: str
    source: str
    tools: tuple[str, ...]  # the agent's function tools, each declared an effect


def _snake(name: str) -> str:
    """``SupportAgent`` as ``support_agent``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return spaced.lower()


def _literal(value: Any) -> str:
    """``value`` as Python source, with strings in double quotes as the rest of the file
    has them. The values are those a JSON schema can ask for."""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    return repr(value)


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
            f"{reference} is not a class of agent (a subclass of livekit.agents.Agent)"
        )
    return module, name, target


def _needs(agent: type[lk.Agent]) -> list[inspect.Parameter]:
    """The arguments ``agent`` cannot be constructed without."""
    wanted = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    parameters = list(inspect.signature(agent.__init__).parameters.values())[1:]  # not self
    return [
        p
        for p in parameters
        if p.default is inspect.Parameter.empty
        and p.kind in (*wanted, inspect.Parameter.KEYWORD_ONLY)
    ]


def _tools(agent: type[lk.Agent], needs: list[inspect.Parameter]) -> list[Any]:
    """The agent's function tools, in order of name. An agent that takes no arguments is
    built, so that tools it is given as it is built are found too. One that takes some
    cannot be built here: its tools are those its class defines, read from an instance
    whose constructor has not run."""
    build: Any = agent  # called with no arguments only where it takes none
    found = build().tools if not needs else lk_llm.find_function_tools(object.__new__(agent))
    return sorted((t for t in found if isinstance(t, _TOOLS)), key=lambda t: t.info.name)


def _placeholder(schema: dict[str, Any]) -> Any:
    """A value the tool's schema accepts for one argument. It means nothing."""
    if schema.get("enum"):
        return schema["enum"][0]
    for option in schema.get("anyOf", []):
        if option.get("type") != "null":
            return _placeholder(option)
    kind = schema.get("type")
    return {
        "string": "example",
        "integer": 1,
        "number": 1,
        "boolean": True,
        "array": [],
        "object": {},
    }.get(kind if isinstance(kind, str) else "", "example")


def _arguments(tool: Any) -> dict[str, Any]:
    """A placeholder for each argument the tool requires. None for a tool whose schema
    cannot be read: the file says the arguments are the author's to give."""
    try:
        if isinstance(tool, lk_llm.RawFunctionTool):
            parameters = tool.info.raw_schema.get("parameters", {})
        else:
            schema = lk_llm.utils.build_legacy_openai_schema(tool, internally_tagged=True)
            parameters = schema["parameters"]
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        return {name: _placeholder(properties.get(name, {})) for name in required}
    except Exception:  # noqa: BLE001 - whatever the tool is, a first test is still written
        return {}


def scaffold(reference: str) -> Scaffold:
    """The first test of the agent ``reference`` names, as ``module:Class``. Raises
    ``ValueError`` for a reference that does not name a class of agent."""
    module, name, agent = _resolve(reference)
    needs = _needs(agent)
    tools = _tools(agent, needs)
    names = [tool.info.name for tool in tools]
    scenario = _snake(name)
    uses = ["Case", "Effect", "ScriptedModel", "UserSpeech"] if tools else None
    lines = [
        f'"""A first Earful test of {name}, written by `earful init`. It runs as it is.',
        'Change what the comments mark until it is a conversation your users have."""',
        "",
        f"from earful.livekit import {', '.join(uses or ['Case', 'ScriptedModel', 'UserSpeech'])}",
    ]
    if tools:
        lines += ["from earful.livekit import tool_failed, tool_succeeded"]
    lines += ["", f"from {module} import {name}", ""]

    if tools:
        intent = _literal(names[0].replace("_", "-"))
        lines += [
            "# The scripted model. Rules are tried in order and the first that holds replies.",
            "# This one calls a tool, then says what came of it. Change the tool, its arguments",
            "# and the words to what your agent does with the request below.",
            "model = (",
            "    ScriptedModel()",
            f'    .when(tool_succeeded(), say="That is done.", claims=[{intent}])',
            '    .when(tool_failed(), say="Sorry, I could not do that.")',
            "    .otherwise(",
            '        say="One moment.",',
            f"        call=({_literal(names[0])}, {_literal(_arguments(tools[0]))}),",
            "    )",
            ")",
        ]
    else:
        lines += [
            "# The scripted model. This agent has no tools, so it only talks. Rules are tried",
            "# in order: add `.when(user_said(...), say=...)` rules before `otherwise`.",
            'model = ScriptedModel().otherwise(say="How can I help?")',
        ]
    lines += ["", ""]

    if needs:
        given = ", ".join(
            "None" if p.kind is inspect.Parameter.POSITIONAL_ONLY else f"{p.name}=None"
            for p in needs
        )
        lines += [
            f"def build() -> {name}:",
            f"    # {name} is built with: {', '.join(p.name for p in needs)}.",
            "    # None stands in for each. That works while every tool is an effect below,",
            "    # because no tool body then runs. Give a stand-in for what a tool calls when",
            "    # you take that tool out of `effects`.",
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
            "        # Each tool listed here stands for one durable write. In a run its body is",
            "        # replaced by a write to Earful's backend, so nothing reaches a real",
            "        # service. Keep the tools that change something (book, pay, send). Delete",
            "        # the line of a tool that only reads, and hand the agent a stand-in for",
            "        # what that tool calls.",
            "        effects={",
            *(
                f"            {_literal(tool)}: Effect({_literal(tool.replace('_', '-'))}),"
                for tool in names
            ),
            "        },",
        ]
    lines += [
        "        # What the user says, in seconds from the start. Change the words.",
        "        speeches=[",
        '            UserSpeech(start=0.5, end=2.5, transcript="Please help me with this."),',
        "        ],",
        "        policy=model,",
        "    )",
        "",
        "",
        "def test_the_conversation_as_written(earful):",
        f"    earful.run({scenario}).assert_contracts()",
        "",
        "",
        "def test_every_timing_and_fault_near_it(earful):",
        f"    earful.explore({scenario}).assert_contracts()",
    ]
    return Scaffold(f"test_{scenario}.py", "\n".join(lines) + "\n", tuple(names))


__all__ = ["Scaffold", "scaffold"]
