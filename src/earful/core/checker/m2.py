"""M2. Turn integrity: each model call is shown the conversation that happened, in order."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import policy as policies
from .memory import Alignment, Item, UserTurns, align, user_turns
from .report import ContractResult, Finding, result, verdict
from .view import Event, Speech, Spoken, ToolInvocation, View

_TOOL_ITEMS = {"function_call": "call", "function_call_output": "result"}


@dataclass
class _Context:
    """A model context or the final history, and what the rules find in it."""

    label: str
    event: Event
    items: list[Item]
    delivered: list[Speech]  # the transcripts delivered before it was requested
    turns: UserTurns
    findings: list[Finding] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)  # code, why not compared
    used: list[Event | None] = field(default_factory=list)

    @property
    def present(self) -> set[int]:
        return {s.index for s in self.turns.present}

    def places(self, kind: str, call_id: str) -> list[int]:
        return [
            index
            for index, item in enumerate(self.items)
            if item["type"] == kind and item["call_id"] == call_id
        ]


def _user_text(context: _Context) -> None:
    """Rule a: the user text is the delivered transcripts, whole and in order."""
    if context.turns.stray:
        context.findings.append(
            Finding(
                "rule a",
                "user text that is no delivered transcript in order: "
                + repr(" ".join(context.turns.stray)),
                (context.event["seq"],),
            )
        )


def _no_loss(view: View, policy: policies.M2, context: _Context) -> None:
    """Rule b: a delivered transcript is not lost once the user's turn can be over."""
    event = context.event
    for speech in context.delivered:
        if speech.index in context.present or speech.backchannel:
            continue
        context.used.append(speech.delivery)
        late = event["t_us"] - view.turn_over_us(speech)
        if late > policy.user_commit_lag_us:
            context.findings.append(
                Finding(
                    "rule b",
                    f"the turn of speech {speech.index} was over {late} us before "
                    "this context, which does not hold its transcript",
                    (speech.delivery["seq"], event["seq"]),
                )
            )


def _replies(context: _Context, alignment: Alignment) -> None:
    """Rule c: the assistant messages are the scripted speech, one to one and in order."""
    if not alignment.possible:
        texts = [repr(context.items[i].get("text")) for i in alignment.unmatched_items]
        context.findings.append(
            Finding(
                "rule c",
                "assistant message(s) that are not what the agent was scripted "
                "to say, in order: " + ", ".join(texts),
                (context.event["seq"],),
            )
        )


def _tool_call(
    context: _Context, tool: ToolInvocation, call_id: str, later_turn: bool, owed: bool
) -> bool:
    """Rule d for one call a tool received; true if the context shows a stand-in result."""
    event = context.event
    done = tool.outcome if tool.outcome is not None and tool.outcome["seq"] < event["seq"] else None
    returned = done if done is not None and done["kind"] == "tool.returned" else None
    calls = context.places("function_call", call_id)
    outputs = context.places("function_call_output", call_id)
    if returned and returned["t_us"] == event["t_us"] and later_turn:
        # Requested for a later turn the instant the tool returned: no order is owed.
        returned = None
    handed_over = bool(returned and returned["data"].get("handoff"))
    if handed_over and not owed and not calls and not outputs:
        return False  # the application builds the context of the agent handed over to
    if not returned and not calls and not outputs:
        return False
    context.used += [tool.called, done]
    seqs = tuple(e["seq"] for e in (tool.called, done, event) if e is not None)
    name = f"{tool.name} ({call_id})"
    state = "finished" if done is not None else "was called"
    # Only a call that returned is owed its call and result; none may be shown twice.
    if len(calls) > 1 or len(outputs) > 1 or (returned and (not calls or not outputs)):
        context.findings.append(
            Finding(
                "rule d",
                f"{name} {state} before this context, which holds "
                f"{len(calls)} call item(s) and {len(outputs)} result item(s) for it",
                seqs,
            )
        )
        return False
    if calls and outputs and outputs[0] < calls[0]:
        context.findings.append(
            Finding("rule d", f"the result of {name} comes before its call", seqs)
        )
    if not returned:
        # A result shown for a running or cancelled call is a stand-in and is not judged.
        return bool(outputs) and (done is None or done["kind"] == "tool.cancelled")
    shown = context.items[outputs[0]]
    if shown["is_error"] and handed_over:
        return True  # the framework's account of a handoff it did not make
    if shown["is_error"]:
        context.findings.append(
            Finding(
                "rule d", f"{name} returned normally, but its result is marked as an error", seqs
            )
        )
    if returned["data"]["text"] is None:
        context.unknown.append(
            ("no-stable-text", f"the value {name} returned has no stable text form to compare")
        )
    elif shown["output"] != returned["data"]["text"]:
        context.findings.append(
            Finding(
                "rule d",
                f"{name} returned {returned['data']['text']!r}, but the context "
                f"shows {shown['output']!r}",
                seqs,
            )
        )
    return False


def _owes_a_handoff(view: View, context: _Context, tool: ToolInvocation) -> bool:
    """Whether ``context`` is owed a call that handed the session over: the model call that
    follows it up, if it asked for one, and the final history."""
    done = tool.outcome
    if context.event["kind"] == "history.final" or done is None or not done["data"].get("handoff"):
        return True
    later = [c.request["seq"] for c in view.calls.values() if c.request["seq"] > done["seq"]]
    return bool(done["data"]["expects_reply"]) and context.event["seq"] == min(later, default=None)


def _tool_calls(view: View, context: _Context, by_request: dict[int, _Context]) -> int:
    """Rule d: every tool call that returned is shown once, with its result. Counts stand-ins."""
    stand_ins = 0
    for tool in view.tools.values():
        if tool.call_id is None or tool.called["seq"] > context.event["seq"]:
            continue
        asked = view.request_of.get(tool.call_id)
        asked_in = by_request[asked.request["seq"]] if asked is not None else None
        later_turn = asked_in is not None and bool(context.present - asked_in.present)
        owed = _owes_a_handoff(view, context, tool)
        stand_ins += _tool_call(context, tool, tool.call_id, later_turn, owed)
    return stand_ins


def _unrun_results(view: View, context: _Context) -> int:
    """Rule d: a result for a call no tool had received. Counts the calls the framework refused."""
    refused = 0
    event = context.event
    for index, item in enumerate(context.items):
        if item["type"] != "function_call_output":
            continue
        invocation = view.invocation_of.get(item["call_id"])
        if invocation is not None and invocation.called["seq"] < event["seq"]:
            continue  # judged with its call
        if invocation is None and item["is_error"]:
            # The framework answered a call no tool received: its own account, not judged.
            refused += 1
        elif invocation is None and item.get("name") not in view.witnessed_tools:
            context.unknown.append(
                (
                    "tool-unwitnessed",
                    f"no witnessed tool call is behind result {item['call_id']!r}",
                )
            )
        else:
            context.findings.append(
                Finding(
                    "rule d",
                    f"item {index} is a result for {item['call_id']!r}, a call no "
                    "witnessed tool had received",
                    (event["seq"],),
                )
            )
    return refused


def _after_its_turns(
    context: _Context, index: int, what: str, source: Event, shown: _Context, why: str
) -> None:
    """Rule e: the item at ``index`` comes after each user turn the model was ``shown``."""
    for speech in shown.turns.present:
        ended = context.turns.ended_at.get(speech.index)
        if ended is not None and index < ended:
            context.findings.append(
                Finding(
                    "rule e",
                    f"{what} appears before speech {speech.index}, {why}",
                    (source["seq"], context.event["seq"]),
                )
            )


def _causal_order(
    view: View,
    context: _Context,
    alignment: Alignment,
    spoken: list[Spoken],
    by_request: dict[int, _Context],
) -> None:
    """Rule e: a reply and a tool item come after the user turns the model answered from."""
    calls_by_key = {s.key: s.call for s in spoken if s.call is not None}
    for key, index in alignment.matched.items():
        call = calls_by_key.get(key)
        if call is None:
            continue  # text the application spoke answers no particular turn
        source = view.calls[call].request
        shown = by_request[source["seq"]]
        _after_its_turns(
            context,
            index,
            f"the reply of call {call}",
            source,
            shown,
            "which it was generated from",
        )
    for index, item in enumerate(context.items):
        if item["type"] not in _TOOL_ITEMS:
            continue
        call_id = item["call_id"]
        asked = view.request_of.get(call_id)
        if asked is None or asked.request["seq"] >= context.event["seq"]:
            continue  # no call the model had made: rule f
        what = f"item {index}, the {_TOOL_ITEMS[item['type']]} of "
        what += f"{view.asked[call_id]['name']} ({call_id!r})"
        shown = by_request[asked.request["seq"]]
        _after_its_turns(
            context, index, what, asked.request, shown, "which the model was shown when it asked"
        )


def _played_before(view: View, context: _Context, alignment: Alignment) -> None:
    """Rule e: what finished playing before a user turn began comes before that turn."""
    if view.origin is None:
        return  # no true start for any speech
    for key, index in alignment.matched.items():
        playout = view.playouts[key]
        done = playout.settled
        if done is None or playout.unknown:
            continue
        for speech in context.turns.present:
            gap = speech.segment_start - done["t_us"]
            if speech.index not in context.turns.placed or gap <= 0:
                continue
            if context.turns.began_at[speech.index] < index:
                context.used += [done, speech.delivery]
                context.findings.append(
                    Finding(
                        "rule e",
                        f"{key} appears after speech {speech.index}, which began {gap} us "
                        "after it finished playing",
                        (done["seq"], speech.delivery["seq"], context.event["seq"]),
                    )
                )


def _same_arguments(shown: str, asked: str) -> bool:
    """Whether two argument texts are equal, as text or as JSON written out again."""
    if shown == asked:
        return True
    try:
        return bool(json.loads(shown) == json.loads(asked))
    except ValueError:
        return False


def _as_asked(view: View, context: _Context) -> None:
    """Rule f: a call item or a result item is of a call the model made, as it made it."""
    event = context.event
    for index, item in enumerate(context.items):
        if item["type"] not in _TOOL_ITEMS:
            continue
        call_id = item["call_id"]
        asked = view.request_of.get(call_id)
        if asked is None or asked.answer["seq"] > event["seq"]:
            if item["type"] == "function_call":
                context.findings.append(
                    Finding(
                        "rule f",
                        f"item {index} is {item['name']} ({call_id!r}), a call the model "
                        "had not made",
                        (event["seq"],),
                    )
                )
            continue  # a result for such a call is rule d's
        made = view.asked[call_id]
        context.used.append(asked.answer)
        seqs = (asked.answer["seq"], event["seq"])
        if item["type"] == "function_call":
            if item["name"] != made["name"] or not _same_arguments(
                item["arguments"], made["arguments"]
            ):
                context.findings.append(
                    Finding(
                        "rule f",
                        f"item {index} shows the call {call_id!r} as {item['name']} with "
                        f"arguments {item['arguments']!r}; the model asked for {made['name']} "
                        f"with {made['arguments']!r}",
                        seqs,
                    )
                )
        elif item["name"] is not None and item["name"] != made["name"]:
            context.findings.append(
                Finding(
                    "rule f",
                    f"item {index} is a result named {item['name']!r} for the call {call_id!r}, "
                    f"which the model made to {made['name']!r}",
                    seqs,
                )
            )


def check(view: View, policy: policies.M2) -> ContractResult:
    contexts = []
    for label, event, items in view.contexts:
        delivered = [s for s in view.delivered if s.delivery["seq"] < event["seq"]]
        turns = user_turns(items, delivered)
        contexts.append(_Context(label, event, items, delivered, turns, used=[event]))
    by_request = {c.event["seq"]: c for c in contexts}

    verdicts = []
    for context in contexts:
        spoken = [s for s in view.spoken if s.born["seq"] < context.event["seq"]]
        alignment = align(context.items, spoken)
        _user_text(context)
        _no_loss(view, policy, context)
        _replies(context, alignment)
        stand_ins = _tool_calls(view, context, by_request)
        refused = _unrun_results(view, context)
        _causal_order(view, context, alignment, spoken, by_request)
        _played_before(view, context, alignment)
        _as_asked(view, context)
        verdicts.append(
            verdict(
                "M2",
                {"context": context.label},
                f"{context.label} shows the conversation that happened"
                if not context.findings
                else f"{context.label} does not show the conversation that happened",
                events=context.used,
                findings=context.findings,
                missing=context.unknown,
                measures={
                    "user_turns": len(context.present),
                    "said_matched": len(alignment.matched),
                    "said_ambiguous": len(alignment.ambiguous),
                    "stand_in_results": stand_ins,
                    "refused_calls": refused,
                },
            )
        )
    return result("M2", policy, verdicts)
