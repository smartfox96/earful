"""Record failures inside scripted providers before the framework handles them."""

from __future__ import annotations

import functools
from collections.abc import Callable, Coroutine
from contextvars import ContextVar, Token
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

# The harness errors of the run in progress. Tasks the framework starts inherit it.
_errors: ContextVar[list[str] | None] = ContextVar("earful_harness_errors", default=None)


def record_errors_in(errors: list[str] | None) -> Token[list[str] | None]:
    """Record what Earful's own providers raise from here on in ``errors``."""
    return _errors.set(errors)


def stop_recording_errors(token: Token[list[str] | None]) -> None:
    _errors.reset(token)


def _record(what: str, exc: Exception) -> None:
    errors, said = _errors.get(), f"{type(exc).__name__}: {exc}"
    # A policy that raised is already there, recorded by the scripted model.
    if errors is not None and not (errors and errors[-1].endswith(said)):
        errors.append(f"the scripted {what} raised {said}")


def scripted(
    what: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """The framework handles what a provider raises as that provider failing. In Earful's
    own providers it is a broken simulation, so it is recorded as a harness error first."""

    def guard(run: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(run)
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await run(*args, **kwargs)
            except Exception as exc:
                _record(what, exc)
                raise

        return guarded

    return guard


def scripted_call(what: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """``scripted`` for a method that is not a coroutine."""

    def guard(call: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(call)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return call(*args, **kwargs)
            except Exception as exc:
                _record(what, exc)
                raise

        return guarded

    return guard
