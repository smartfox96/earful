"""Recognize a realtime session wired to the selected agent in its own module.

Trace constructor bindings to session starts, including local factories and branches.
Source is evidence of a wiring the application defines.
Other arrangements need the author's declaration. Never execute the entry code.
"""

from __future__ import annotations

import ast
import inspect
from functools import lru_cache
from types import ModuleType

import livekit.agents as lk
from livekit.agents import llm

from ..core.search import CaseRefused

_Function = ast.FunctionDef | ast.AsyncFunctionDef
_Binding = str | type[lk.Agent] | type[llm.RealtimeModel] | None
_Bindings = dict[str, _Binding]


def _symbol(node: ast.expr, module: ModuleType, locals_: set[str]) -> object:
    if isinstance(node, ast.Name):
        return None if node.id in locals_ else vars(module).get(node.id)
    if isinstance(node, ast.Attribute):
        parent = _symbol(node.value, module, locals_)
        if isinstance(parent, ModuleType):
            return vars(parent).get(node.attr)
    return None


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _wiring(
    function: _Function,
    module: ModuleType,
    agent: type[lk.Agent],
    factories: dict[str, str],
) -> tuple[str, list[str]]:
    """A factory's returned model and the models of sessions that start this agent.

    An empty model name means that the session's model is unknown or is a cascade.
    """
    started: list[str] = []
    locals_ = {n.arg for n in ast.walk(function) if isinstance(n, ast.arg)}
    locals_.update(
        n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    )
    locals_.update(
        (n.asname or n.name.split(".")[0]) for n in ast.walk(function) if isinstance(n, ast.alias)
    )

    def value(node: ast.expr, bindings: _Bindings) -> _Binding:
        if not isinstance(node, ast.Call):
            return bindings.get(_name(node))
        constructor = node.func.value if isinstance(node.func, ast.Subscript) else node.func
        target = _symbol(constructor, module, locals_)
        if target is agent:
            return agent
        if isinstance(target, type) and issubclass(target, llm.RealtimeModel):
            return target
        if target is lk.AgentSession:
            model = next(
                (value(kw.value, bindings) for kw in node.keywords if kw.arg == "llm"), None
            )
            if isinstance(model, type) and issubclass(model, llm.RealtimeModel):
                return f"{model.__module__}.{model.__qualname__}"
            return ""
        name = _name(node.func)
        return None if name in locals_ else factories.get(name)

    returned: list[_Binding] = []

    def visit(body: list[ast.stmt], bindings: _Bindings) -> _Bindings | None:
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                result = value(stmt.value, bindings)
                for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    if key := _name(target):
                        bindings[key] = result
            elif isinstance(stmt, ast.Return):
                returned.append(value(stmt.value, bindings) if stmt.value else None)
                return None
            elif isinstance(stmt, ast.Raise):
                return None
            elif isinstance(stmt, ast.Expr):
                expr = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
                if (
                    isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Attribute)
                    and expr.func.attr == "start"
                ):
                    selected = next((kw.value for kw in expr.keywords if kw.arg == "agent"), None)
                    if selected is None and expr.args:
                        selected = expr.args[0]
                    if selected is not None and value(selected, bindings) is agent:
                        session = value(expr.func.value, bindings)
                        started.append(session if isinstance(session, str) else "")
            elif isinstance(stmt, (ast.If, ast.Try)):
                if isinstance(stmt, ast.If):
                    paths = [visit(branch, bindings.copy()) for branch in (stmt.body, stmt.orelse)]
                else:
                    normal = visit(stmt.body, bindings.copy())
                    paths = [visit(stmt.orelse, normal) if normal is not None else None]
                    # An exception may have interrupted any assignment in the try block.
                    exceptional = bindings.copy()
                    _forget_assignments(stmt.body, exceptional)
                    paths.extend(
                        visit(handler.body, exceptional.copy()) for handler in stmt.handlers
                    )
                live = [path for path in paths if path is not None]
                if not live:
                    return None
                bindings = {k: v for k, v in live[0].items() if all(p.get(k) == v for p in live)}
                if isinstance(stmt, ast.Try) and stmt.finalbody:
                    after = visit(stmt.finalbody, bindings)
                    if after is None:
                        return None
                    bindings = after
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                after = visit(stmt.body, bindings)
                if after is None:
                    return None
                bindings = after
            elif not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _forget_assignments([stmt], bindings)
        return bindings

    visit(function.body, {})
    model = returned[0] if returned else None
    return model if isinstance(model, str) and all(v == model for v in returned) else "", started


def _forget_assignments(body: list[ast.stmt], bindings: _Bindings) -> None:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Store):
                bindings.pop(_name(node), None)


@lru_cache(maxsize=64)
def _functions(source: str) -> list[tuple[str, list[_Function]]]:
    body = ast.parse(source).body
    groups = [
        ("", body),
        *(("self.", node.body) for node in body if isinstance(node, ast.ClassDef)),
    ]
    return [
        (
            prefix,
            [node for node in group if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))],
        )
        for prefix, group in groups
    ]


def realtime_session(agent: type[lk.Agent]) -> str:
    """The name of a realtime model in a recognized start of this agent, or empty."""
    module = inspect.getmodule(agent)
    if module is None or agent is lk.Agent or vars(module).get(agent.__name__) is not agent:
        return ""
    try:
        source = inspect.getsource(module)
        if ".start" not in source:
            return ""
        groups = _functions(source)
    except (OSError, TypeError, SyntaxError):
        return ""
    starts: list[str] = []
    for prefix, functions in groups:
        factories = {}
        for function in functions:
            returned, _ = _wiring(function, module, agent, {})
            if returned:
                factories[prefix + function.name] = returned
        for function in functions:
            _, started = _wiring(function, module, agent, factories)
            starts.extend(started)
    return starts[0] if starts and all(starts) else ""


def check_session(agent: type[lk.Agent], scripted_turns: bool, *, declaration: str) -> None:
    if model := realtime_session(agent):
        raise CaseRefused(
            f"{agent.__name__} is started with the speech-to-speech model {model}. "
            "Earful runs a separate recognizer, model and voice."
        )
    if scripted_turns is not True:
        raise CaseRefused(
            "Earful builds its own LiveKit session and cannot verify the application's "
            "turn detector. For a separate recognizer, model and voice, explicitly choose "
            f"silence-based turn detection with {declaration}. Speech-to-speech is unsupported."
        )


def realtime_provider_refusal(provider: object) -> str:
    if isinstance(provider, llm.RealtimeModel):
        model = type(provider)
        return (
            f"{model.__module__}.{model.__qualname__} is a speech-to-speech model. "
            "Earful runs a separate recognizer, model and voice."
        )
    return ""
