"""Recognize a realtime session wired to the selected agent in its own module.

Trace constructor bindings to session starts, including local factories and branches.
Source is evidence of a wiring the application defines.
Other arrangements need the author's declaration. Never execute the entry code.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Callable
from functools import lru_cache
from types import ModuleType

import livekit.agents as lk
from livekit.agents import llm

from ..core.search import CaseRefused

_Function = ast.FunctionDef | ast.AsyncFunctionDef
_Binding = str | type[lk.Agent] | type[llm.RealtimeModel] | None
_Values = frozenset[_Binding]
_Bindings = dict[str, _Values]
_UNKNOWN: _Values = frozenset({None})


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
    factory: Callable[[str, bool], _Values],
) -> tuple[_Values, list[str]]:
    """Possible return values and the models of sessions that start this agent.

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
    locals_.update(
        n.name
        for n in ast.walk(function)
        if n is not function and isinstance(n, _Function | ast.ClassDef)
    )

    def value(node: ast.expr, bindings: _Bindings, *, awaited: bool = False) -> _Values:
        if isinstance(node, ast.Await):
            return value(node.value, bindings, awaited=True)
        if isinstance(node, ast.IfExp):
            if isinstance(node.test, ast.Constant):
                return value(
                    node.body if node.test.value else node.orelse, bindings, awaited=awaited
                )
            return value(node.body, bindings, awaited=awaited) | value(
                node.orelse, bindings, awaited=awaited
            )
        if not isinstance(node, ast.Call):
            return bindings.get(_name(node), _UNKNOWN)
        constructor = node.func.value if isinstance(node.func, ast.Subscript) else node.func
        target = _symbol(constructor, module, locals_)
        if target is agent:
            return frozenset({agent})
        if isinstance(target, type) and issubclass(target, llm.RealtimeModel):
            return frozenset({target})
        if target is lk.AgentSession:
            models = next(
                (value(kw.value, bindings) for kw in node.keywords if kw.arg == "llm"), _UNKNOWN
            )
            return frozenset(
                f"{model.__module__}.{model.__qualname__}"
                if isinstance(model, type) and issubclass(model, llm.RealtimeModel)
                else ""
                for model in models
            )
        name = _name(node.func)
        return _UNKNOWN if name in locals_ else factory(name, awaited)

    def record_start(expr: ast.expr, bindings: _Bindings) -> None:
        if isinstance(expr, ast.Await):
            expr = expr.value
        if not isinstance(expr, ast.Call):
            return
        if _symbol(expr.func, module, locals_) is asyncio.create_task:
            coro = next((kw.value for kw in expr.keywords if kw.arg == "coro"), None)
            if coro is None and expr.args:
                coro = expr.args[0]
            if coro is not None:
                record_start(coro, bindings)
        elif isinstance(expr.func, ast.Attribute) and expr.func.attr == "start":
            selected = next((kw.value for kw in expr.keywords if kw.arg == "agent"), None)
            if selected is None and expr.args:
                selected = expr.args[0]
            if selected is not None and value(selected, bindings) == {agent}:
                sessions = value(expr.func.value, bindings)
                started.extend(session if isinstance(session, str) else "" for session in sessions)

    returned: set[_Binding] = set()

    def visit(body: list[ast.stmt], bindings: _Bindings) -> _Bindings | None:
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)) and stmt.value:
                record_start(stmt.value, bindings)
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                result = value(stmt.value, bindings)
                for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    if key := _name(target):
                        bindings[key] = result
            elif isinstance(stmt, ast.Return):
                returned.update(value(stmt.value, bindings) if stmt.value else _UNKNOWN)
                return None
            elif isinstance(stmt, (ast.Raise, ast.Break, ast.Continue)):
                return None
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                if (
                    isinstance(stmt, ast.While)
                    and isinstance(stmt.test, ast.Constant)
                    and not stmt.test.value
                ):
                    after = visit(stmt.orelse, bindings)
                    if after is None:
                        return None
                    bindings = after
                    continue
                # The loop may run zero times. At the next iteration, any name
                # written in the body or by the loop target may have changed.
                _forget_assignments([stmt], bindings)
                visit(stmt.body, bindings.copy())
                visit(stmt.orelse, bindings.copy())
            elif isinstance(stmt, (ast.If, ast.Try)):
                if isinstance(stmt, ast.If):
                    branches = [stmt.body, stmt.orelse]
                    if isinstance(stmt.test, ast.Constant):
                        branches = [stmt.body if stmt.test.value else stmt.orelse]
                    paths = [visit(branch, bindings.copy()) for branch in branches]
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
                bindings = {
                    k: live[0][k].union(*(p.get(k, _UNKNOWN) for p in live[1:])) for k in live[0]
                }
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

    if visit(function.body, {}) is not None:
        returned.add(None)  # An implicit return is another possible factory result.
    return frozenset(returned) or _UNKNOWN, started


def _forget_assignments(body: list[ast.stmt], bindings: _Bindings) -> None:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Store):
                bindings.pop(_name(node), None)


@lru_cache(maxsize=64)
def _function_scopes(source: str) -> dict[_Function, dict[str, _Function]]:
    body = ast.parse(source).body
    helpers = {f.name: f for f in body if isinstance(f, _Function)}
    scopes = {f: helpers for f in helpers.values()}
    for node in body:
        if isinstance(node, ast.ClassDef):
            methods = {f"self.{f.name}": f for f in node.body if isinstance(f, _Function)}
            visible = helpers | methods
            scopes.update((method, visible) for method in methods.values())
    return scopes


def realtime_session(agent: type[lk.Agent]) -> str:
    """The name of a realtime model in a recognized start of this agent, or empty."""
    module = inspect.getmodule(agent)
    if module is None or agent is lk.Agent or vars(module).get(agent.__name__) is not agent:
        return ""
    try:
        source = inspect.getsource(module)
        if ".start" not in source:
            return ""
        scopes = _function_scopes(source)
    except (OSError, TypeError, SyntaxError):
        return ""

    summaries: dict[_Function, tuple[_Values, list[str]]] = {}

    def wiring(function: _Function) -> tuple[_Values, list[str]]:
        def factory(name: str, awaited: bool) -> _Values:
            helper = scopes[function].get(name)
            if helper is None or isinstance(helper, ast.AsyncFunctionDef) != awaited:
                return _UNKNOWN
            return wiring(helper)[0]

        if function not in summaries:
            summaries[function] = (_UNKNOWN, [])  # Recursive calls have unknown results.
            summaries[function] = _wiring(function, module, agent, factory)
        return summaries[function]

    starts = {model for function in scopes for model in wiring(function)[1]}
    return " or ".join(sorted(starts)) if all(starts) else ""


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
