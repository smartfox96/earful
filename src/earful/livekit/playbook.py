"""A scripted model written as rules: what the model is shown, and what it then says."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .providers import Claim, Reply, ToolCall


@dataclass(frozen=True)
class ToolResult:
    """One tool result in the context, as the framework presented it to the model."""

    name: str | None
    output: str
    call_id: str
    failed: bool  # the framework marked it as an error


@dataclass(frozen=True)
class Context:
    """What the model is shown for one call. ``items`` are the summaries a policy receives."""

    items: tuple[Mapping[str, Any], ...]

    @classmethod
    def of(cls, items: Iterable[Mapping[str, Any]]) -> Context:
        return cls(tuple(items))

    def _texts(self, role: str) -> tuple[str, ...]:
        return tuple(
            item.get("text") or ""
            for item in self.items
            if item["type"] == "message" and item.get("role") == role
        )

    @property
    def user_turns(self) -> tuple[str, ...]:
        """The text of every user message, oldest first."""
        return self._texts("user")

    @property
    def last_user_turn(self) -> str:
        """The text of the latest user message; empty if there is none."""
        turns = self.user_turns
        return turns[-1] if turns else ""

    @property
    def assistant_turns(self) -> tuple[str, ...]:
        """The text of every assistant message, oldest first."""
        return self._texts("assistant")

    @property
    def tool_results(self) -> tuple[ToolResult, ...]:
        """Every tool result, oldest first."""
        return tuple(
            ToolResult(
                name=item.get("name"),
                output=item.get("output") or "",
                call_id=item.get("call_id") or "",
                failed=bool(item.get("is_error")),
            )
            for item in self.items
            if item["type"] == "function_call_output"
        )

    def results_of(self, tool: str | None = None) -> tuple[ToolResult, ...]:
        """The results of one tool, or of every tool if none is named."""
        return tuple(r for r in self.tool_results if tool is None or r.name == tool)


Test = Callable[[Context], bool]


@dataclass(frozen=True)
class Condition:
    """A named test of a context. Combine with ``&``, ``|`` and ``~``."""

    test: Test
    name: str

    def __call__(self, context: Context) -> bool:
        return bool(self.test(context))

    def __and__(self, other: Test) -> Condition:
        return Condition(lambda c: self(c) and other(c), f"({self.name} and {_name(other)})")

    def __or__(self, other: Test) -> Condition:
        return Condition(lambda c: self(c) or other(c), f"({self.name} or {_name(other)})")

    def __invert__(self) -> Condition:
        return Condition(lambda c: not self(c), f"not {self.name}")


def _name(test: Test) -> str:
    return test.name if isinstance(test, Condition) else getattr(test, "__name__", "a condition")


def _heard(text: str, phrases: Sequence[str]) -> bool:
    text = text.lower()
    return any(phrase.lower() in text for phrase in phrases)


def user_said(*phrases: str) -> Condition:
    """The latest user message contains one of ``phrases``, whatever its case."""
    if not phrases:
        raise ValueError("user_said takes at least one phrase")
    return Condition(lambda c: _heard(c.last_user_turn, phrases), f"user_said{phrases!r}")


def user_ever_said(*phrases: str) -> Condition:
    """Some user message in the context contains one of ``phrases``, whatever its case."""
    if not phrases:
        raise ValueError("user_ever_said takes at least one phrase")
    return Condition(
        lambda c: any(_heard(turn, phrases) for turn in c.user_turns),
        f"user_ever_said{phrases!r}",
    )


def tool_returned(tool: str | None = None) -> Condition:
    """The context holds a result of ``tool``, or of any tool: a success or an error."""
    return Condition(lambda c: bool(c.results_of(tool)), f"tool_returned({tool or ''})")


def tool_succeeded(tool: str | None = None) -> Condition:
    """The context holds a result of ``tool``, or of any tool, that is not an error."""
    return Condition(
        lambda c: any(not r.failed for r in c.results_of(tool)), f"tool_succeeded({tool or ''})"
    )


def tool_failed(tool: str | None = None) -> Condition:
    """The context holds results of ``tool``, or of any tool, and every one is an error."""
    return Condition(
        lambda c: bool(c.results_of(tool)) and all(r.failed for r in c.results_of(tool)),
        f"tool_failed({tool or ''})",
    )


Call = ToolCall | tuple[str, Mapping[str, Any]] | str


def _tool_calls(call: Call | Sequence[Call]) -> tuple[ToolCall, ...]:
    if isinstance(call, ToolCall):
        return (call,)
    if isinstance(call, str):
        return (ToolCall(call, "{}"),)
    # A name with its arguments is one call, though a pair is also a sequence.
    if (
        isinstance(call, tuple)
        and len(call) == 2
        and isinstance(call[0], str)
        and isinstance(call[1], Mapping)
    ):
        return (ToolCall(call[0], json.dumps(dict(call[1]))),)
    return tuple(one for each in call for one in _tool_calls(each))


def reply(
    say: str = "",
    *,
    call: Call | Sequence[Call] = (),
    claims: Sequence[str | Claim] = (),
    ttft: float | None = None,
    duration: float | None = None,
) -> Reply:
    """A scripted reply from plain values. ``call`` is a tool's name, a name with its
    arguments, a ``ToolCall``, or several of these. A claim is an intent's name, for the
    whole text, or a ``Claim``. ``ttft`` and ``duration`` are in seconds."""
    times = {
        name: value for name, value in (("ttft", ttft), ("duration", duration)) if value is not None
    }
    return Reply(
        text=say,
        tool_calls=_tool_calls(call),
        claims=tuple(c if isinstance(c, Claim) else Claim(c) for c in claims),
        **times,
    )


@dataclass(frozen=True)
class Rule:
    when: Test
    reply: Reply


class NoRule(LookupError):
    """No rule of a scripted model matches the context it was shown."""


@dataclass(frozen=True)
class ScriptedModel:
    """A model policy as ordered rules: the first whose condition holds gives the reply.
    ``when`` and ``otherwise`` return a new model. A call raises ``NoRule`` if none matches."""

    rules: tuple[Rule, ...] = ()
    default: Reply | None = None

    def when(
        self,
        condition: Test,
        answer: Reply | None = None,
        *,
        say: str = "",
        call: Call | Sequence[Call] = (),
        claims: Sequence[str | Claim] = (),
        ttft: float | None = None,
        duration: float | None = None,
    ) -> ScriptedModel:
        """This model with one more rule, tried after those it already has."""
        if self.default is not None:
            raise ValueError("a rule added after otherwise() could never be reached")
        given = _given(answer, say, call, claims, ttft, duration)
        return ScriptedModel((*self.rules, Rule(condition, given)), None)

    def otherwise(
        self,
        answer: Reply | None = None,
        *,
        say: str = "",
        call: Call | Sequence[Call] = (),
        claims: Sequence[str | Claim] = (),
        ttft: float | None = None,
        duration: float | None = None,
    ) -> ScriptedModel:
        """This model with the reply to give when no rule matches."""
        if self.default is not None:
            raise ValueError("otherwise() was already given")
        return ScriptedModel(self.rules, _given(answer, say, call, claims, ttft, duration))

    def __call__(self, items: Iterable[Mapping[str, Any]]) -> Reply:
        context = Context.of(items)
        for rule in self.rules:
            if rule.when(context):
                return rule.reply
        if self.default is not None:
            return self.default
        tried = ", ".join(_name(rule.when) for rule in self.rules) or "none"
        raise NoRule(
            f"no rule matches a context whose latest user message is "
            f"{context.last_user_turn!r} and which holds {len(context.tool_results)} tool "
            f"result(s); rules: {tried}. Add a rule for it, or otherwise()"
        )


def _given(
    answer: Reply | None,
    say: str,
    call: Call | Sequence[Call],
    claims: Sequence[str | Claim],
    ttft: float | None,
    duration: float | None,
) -> Reply:
    spelled = bool(say or call or claims or ttft is not None or duration is not None)
    if answer is not None and spelled:
        raise ValueError("give a Reply, or the parts of one, not both")
    if answer is None and not spelled:
        raise ValueError("a rule needs a reply: say=, call=, or a Reply")
    return (
        answer
        if answer is not None
        else reply(say, call=call, claims=claims, ttft=ttft, duration=duration)
    )


__all__ = [
    "Call",
    "Condition",
    "Context",
    "NoRule",
    "Rule",
    "ScriptedModel",
    "ToolResult",
    "reply",
    "tool_failed",
    "tool_returned",
    "tool_succeeded",
    "user_ever_said",
    "user_said",
]
