"""``earful init`` for a Pipecat bot: a first test, written from the functions it registers."""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any

import jsonschema
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import LLMService

from ..scripted.scaffold import (
    Scaffold,
    effects_lines,
    literal,
    model_lines,
    placeholder,
    select_writes,
    tests_lines,
)
from . import seams
from .case import bot_context

_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class _Reader(LLMService):
    """A model service that only collects what a bot registers on it."""


def _resolve(reference: str) -> tuple[str, str, Any]:
    if not _REFERENCE.match(reference):
        raise ValueError(
            f"a bot is named as module:function, such as mybot.bot:booking_bot; got {reference!r}"
        )
    module, _, name = reference.partition(":")
    try:
        target = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"no bot {reference!r}: {exc}") from exc
    if inspect.isclass(target) or not callable(target):
        raise ValueError(
            f"{reference} is not a function. For Pipecat, `earful init` takes the function that "
            "registers the bot's functions on its model service and returns its LLMContext"
        )
    return module, name, target


def _needs(bot: Any) -> list[inspect.Parameter]:
    """What the bot cannot be called without, after the model service it is handed first."""
    parameters = list(inspect.signature(bot).parameters.values())
    if not parameters:
        raise ValueError(f"{bot.__name__} takes no model service: a bot is called as bot(llm)")
    return [
        p
        for p in parameters[1:]
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]


def _given(needs: list[inspect.Parameter]) -> tuple[list[Any], dict[str, Any]]:
    positional = [None for p in needs if p.kind is inspect.Parameter.POSITIONAL_ONLY]
    named = {p.name: None for p in needs if p.kind is not inspect.Parameter.POSITIONAL_ONLY}
    return positional, named


def _functions(bot: Any, needs: list[inspect.Parameter]) -> dict[str, dict[str, Any] | None]:
    """Each function the bot registers or its context advertises, with its parameters where
    the context gives them."""
    reader = _Reader()
    positional, named = _given(needs)
    try:
        context = bot_context(bot, reader, *positional, **named)
    except Exception as exc:
        given = ", ".join(["llm", *("None" for _ in positional), *(f"{k}=None" for k in named)])
        raise ValueError(
            f"{bot.__name__}({given}) raised {type(exc).__name__}: {exc}. `earful init` calls "
            "the bot to read the functions it registers"
        ) from exc
    found: dict[str, dict[str, Any] | None] = {}
    tools = seams.advertised_tools(context)
    for schema in tools.standard_tools if isinstance(tools, ToolsSchema) else []:
        if isinstance(schema, FunctionSchema):
            found[schema.name] = {
                "type": "object",
                "properties": schema.properties,
                "required": schema.required,
            }
    for name in seams.declared_functions(reader):
        found.setdefault(name, None)
    return dict(sorted(found.items()))


def _call(functions: dict[str, dict[str, Any] | None]) -> tuple[str, dict[str, Any]]:
    """The first function a call can be written for, and its arguments."""
    refused = []
    for name, parameters in functions.items():
        if parameters is None:
            return name, {}  # registered by name alone: it takes what the script gives
        arguments: dict[str, Any] = placeholder(parameters, parameters.get("$defs", {}))
        try:
            jsonschema.validate(arguments, parameters)
        except jsonschema.ValidationError as exc:
            refused.append(
                ValueError(
                    f"`earful init` cannot write a call of the function {name!r}: it has no "
                    f"value for the argument {'.'.join(map(str, exc.absolute_path))!r} that it "
                    f"accepts ({exc.message})"
                )
            )
            continue
        return name, arguments
    raise refused[0]


def scaffold(reference: str, *, writes: tuple[str, ...] = ()) -> Scaffold:
    """The first test of the bot ``reference`` names, as ``module:function``. Raises
    ``ValueError`` for a reference that does not name a bot function."""
    module, name, bot = _resolve(reference)
    needs = _needs(bot)
    functions = _functions(bot, needs)
    names = list(functions)
    selected = select_writes(writes, names)
    called, arguments = _call({n: functions[n] for n in selected}) if selected else ("", {})
    scenario = f"{name}_scenario"
    uses = ["Case", "Effect", "ScriptedModel", "UserSpeech"] if selected else None
    lines = [
        f'"""A first Earful test of {name}, written by `earful init`. Edit what is marked."""',
        "",
        f"from earful.pipecat import {', '.join(uses or ['Case', 'ScriptedModel', 'UserSpeech'])}",
    ]
    if selected:
        lines += ["from earful.pipecat import tool_failed, tool_succeeded, user_said"]
    lines += ["", f"from {module} import {name}", ""]
    if selected:
        block = model_lines(called, arguments, noun="function", owner="your bot")
        if functions[called] is None:
            at = next(n for n, line in enumerate(block) if line.startswith("CALL = "))
            block[at:at] = [
                f"# {called} is registered without a schema, so its arguments are not known:",
                "# give CALL the arguments it reads.",
            ]
        lines += block
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
        lines += [
            "def bot(llm):",
            f"    # {name} is called with: {', '.join(p.name for p in needs)}.",
            "    # Supply a stand-in for dependencies your test exercises.",
            f"    return {name}(llm, {given})",
            "",
            "",
        ]
    lines += [
        f"def {scenario}() -> Case:",
        "    return Case(",
        f"        name={literal(name)},",
        f"        bot={'bot' if needs else name},",
    ]
    if selected:
        lines += effects_lines(selected, noun="function", owner="the bot")
    lines += [
        "        # What the user says, in seconds from the start.",
        "        speeches=[",
        '            UserSpeech(start=0.5, end=2.5, transcript="Please help me with this."),',
        "        ],",
        "        policy=model,",
        "    )",
        "",
        "",
        *tests_lines(scenario, ["A2", "A3", "L1"] if selected else ["L1"]),
    ]
    source = "\n".join(lines) + "\n"
    return Scaffold(f"test_{name}.py", source, tuple(names), noun="functions")


__all__ = ["scaffold"]
