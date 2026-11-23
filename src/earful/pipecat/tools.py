"""Record registered function calls and their callbacks at the application boundary."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pipecat.services.llm_service import FunctionCallParams

from ..core.backend import Effect, current_tool
from ..core.ledger import Ledger, json_value


class ToolWitness:
    """Records each call of a function the application registered, as the LiveKit
    adapter's witness does. A Pipecat tool reports through a callback, which may come
    before the handler returns: the result is recorded when the framework is told."""

    def __init__(self, ledger: Ledger, effects: dict[str, Effect]) -> None:
        self._effects = effects
        self._ledger = ledger
        self._invocations = 0
        self.in_flight = 0
        self.exceptions: list[Exception] = []
        self.refusal: str | None = None
        self.names: set[str] = set()

    def wrap(self, handler: Callable[..., Any], *, direct: bool = False) -> Any:
        """``handler`` with its calls recorded. A direct function also takes its arguments
        by name; a registered handler takes the call's parameters alone."""

        @wraps(handler)
        async def witnessed(params: FunctionCallParams, **arguments: Any) -> None:
            await self._call(params, lambda told: handler(told, **arguments))

        @wraps(handler)
        async def witnessed_handler(params: FunctionCallParams) -> None:
            await self._call(params, handler)

        return witnessed if direct else witnessed_handler

    async def _call(
        self, params: FunctionCallParams, run: Callable[[FunctionCallParams], Awaitable[Any]]
    ) -> None:
        self._invocations += 1
        tool = self._invocations
        self._ledger.append(
            "world",
            "tool.called",
            tool=tool,
            name=params.function_name,
            call_id=params.tool_call_id,
            arguments=json_value(dict(params.arguments)),
        )
        report = params.result_callback
        told = False

        async def callback(result: Any, *, properties: Any = None) -> None:
            nonlocal told
            if not told and (properties is None or properties.is_final):
                told = True
                self._ledger.append(
                    "world",
                    "tool.returned",
                    tool=tool,
                    name=params.function_name,
                    output=json_value(result),
                    text=json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else "COMPLETED",
                    expects_reply=properties is None or properties.run_llm is not False,
                )
            await report(result, properties=properties)

        token = current_tool.set(tool)
        self.in_flight += 1
        try:
            declared = self._effects.get(params.function_name)
            if declared is None:
                await run(dataclasses.replace(params, result_callback=callback))
            else:
                await callback(await declared.make(dict(params.arguments)))
        except asyncio.CancelledError:
            if not told:
                self._ledger.append("world", "tool.cancelled", tool=tool, name=params.function_name)
            raise
        except SystemExit as exc:
            self.refusal = f"tool {params.function_name!r} called sys.exit({exc.code!r})"
            raise asyncio.CancelledError(self.refusal) from exc
        except Exception as exc:
            self.exceptions.append(exc)
            if told:
                raise
            self._ledger.append(
                "world",
                "tool.raised",
                tool=tool,
                name=params.function_name,
                error=type(exc).__name__,
                message=str(exc),
                expects_reply=True,
            )
            raise
        finally:
            self.in_flight -= 1
            current_tool.reset(token)
