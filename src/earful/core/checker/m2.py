"""M2. Turn integrity: each model call is shown the conversation that happened, in order."""

from __future__ import annotations

from . import policy as policies
from .memory import align, user_turns
from .report import ContractResult, Finding, result, verdict
from .view import Event, View


def check(view: View, policy: policies.M2) -> ContractResult:
    verdicts = []
    by_call_id = {t.call_id: t for t in view.tools.values() if t.call_id is not None}
    requested_by = {tc["call_id"]: c.call for c in view.calls.values() for tc in c.tool_calls}
    turns_in: dict[int, set[int]] = {}  # model call -> speeches its context held

    for label, event, items in view.contexts:
        findings, unknown = [], []
        used: list[Event | None] = [event]
        delivered = [s for s in view.delivered if s.delivery["seq"] < event["seq"]]

        # Rule a: the user text is the delivered transcripts, whole and in order.
        turns = user_turns(items, delivered)
        present = {s.index for s in turns.present}
        if event["kind"] == "llm.request":
            turns_in[event["data"]["call"]] = present
        if turns.stray:
            findings.append(
                Finding(
                    "rule a",
                    "user text that is no delivered transcript in order: "
                    + repr(" ".join(turns.stray)),
                    (event["seq"],),
                )
            )

        # Rule b: a delivered transcript is not lost once the user's turn can be over.
        for speech in delivered:
            if speech.index in present or speech.backchannel:
                continue
            used.append(speech.delivery)
            late = event["t_us"] - view.turn_over_us(speech)
            if late > policy.user_commit_lag_us:
                findings.append(
                    Finding(
                        "rule b",
                        f"the turn of speech {speech.index} was over {late} us before "
                        "this context, which does not hold its transcript",
                        (speech.delivery["seq"], event["seq"]),
                    )
                )

        # Rule c: the assistant messages are the scripted speech, one to one and in order.
        spoken = [s for s in view.spoken if s.born["seq"] < event["seq"]]
        alignment = align(items, spoken)
        if not alignment.possible:
            texts = [repr(items[i].get("text")) for i in alignment.unmatched_items]
            findings.append(
                Finding(
                    "rule c",
                    "assistant message(s) that are not what the agent was scripted "
                    "to say, in order: " + ", ".join(texts),
                    (event["seq"],),
                )
            )

        # Rule d: every tool call that returned is shown once, with its result.
        stand_ins = refused = 0
        for tool in view.tools.values():
            if tool.call_id is None or tool.called["seq"] > event["seq"]:
                continue
            done = (
                tool.outcome
                if tool.outcome is not None and tool.outcome["seq"] < event["seq"]
                else None
            )
            returned = done if done is not None and done["kind"] == "tool.returned" else None
            calls = [
                i
                for i, it in enumerate(items)
                if it["type"] == "function_call" and it["call_id"] == tool.call_id
            ]
            outputs = [
                i
                for i, it in enumerate(items)
                if it["type"] == "function_call_output" and it["call_id"] == tool.call_id
            ]
            if not returned and not calls and not outputs:
                continue
            asked_in = requested_by.get(tool.call_id)
            if (
                returned
                and returned["t_us"] == event["t_us"]
                and asked_in in turns_in
                and present - turns_in[asked_in]
            ):
                # Requested for a later turn the instant the tool returned: no order is owed.
                returned = None
                if not calls and not outputs:
                    continue
            used += [tool.called, done]
            seqs = tuple(e["seq"] for e in (tool.called, done, event) if e is not None)
            name = f"{tool.name} ({tool.call_id})"
            state = "finished" if done is not None else "was called"
            # Only a call that returned is owed its call and result; none may be shown twice.
            if len(calls) > 1 or len(outputs) > 1 or (returned and (not calls or not outputs)):
                findings.append(
                    Finding(
                        "rule d",
                        f"{name} {state} before this context, which holds "
                        f"{len(calls)} call item(s) and {len(outputs)} result item(s) for it",
                        seqs,
                    )
                )
                continue
            if calls and outputs and outputs[0] < calls[0]:
                findings.append(
                    Finding("rule d", f"the result of {name} comes before its call", seqs)
                )
            if not returned:
                # A result shown for a running or cancelled call is a stand-in and is not judged.
                stand_ins += bool(outputs) and (done is None or done["kind"] == "tool.cancelled")
                continue
            shown = items[outputs[0]]
            if shown["is_error"]:
                findings.append(
                    Finding(
                        "rule d",
                        f"{name} returned normally, but its result is marked as an error",
                        seqs,
                    )
                )
            if returned["data"]["text"] is None:
                unknown.append(f"the value {name} returned has no stable text form to compare")
            elif shown["output"] != returned["data"]["text"]:
                findings.append(
                    Finding(
                        "rule d",
                        f"{name} returned {returned['data']['text']!r}, but the context "
                        f"shows {shown['output']!r}",
                        seqs,
                    )
                )
        for index, item in enumerate(items):
            if item["type"] != "function_call_output":
                continue
            invocation = by_call_id.get(item["call_id"])
            if invocation is not None and invocation.called["seq"] < event["seq"]:
                continue  # judged above
            if invocation is None and item["is_error"]:
                # The framework answered a call no tool received: its own account, not judged.
                refused += 1
            elif invocation is None and item.get("name") not in view.witnessed_tools:
                unknown.append(
                    f"the tool behind result {item['call_id']!r} was not witnessed, "
                    "or cannot be linked to it"
                )
            else:
                findings.append(
                    Finding(
                        "rule d",
                        f"item {index} is a result for {item['call_id']!r}, a call no "
                        "witnessed tool had received",
                        (event["seq"],),
                    )
                )

        # Rule e: a reply comes after the user turns it was generated from.
        calls_by_key = {s.key: s.call for s in spoken if s.call is not None}
        for key, index in alignment.matched.items():
            call = calls_by_key.get(key)
            if call is None:
                continue  # text the application spoke answers no particular turn
            source = view.calls[call]
            seen = user_turns(
                source.items,
                [s for s in view.delivered if s.delivery["seq"] < source.request["seq"]],
            )
            for speech in seen.present:
                ended = turns.ended_at.get(speech.index)
                if ended is not None and index < ended:
                    findings.append(
                        Finding(
                            "rule e",
                            f"the reply of call {call} appears before speech "
                            f"{speech.index}, which it was generated from",
                            (source.request["seq"], event["seq"]),
                        )
                    )

        instance = {"context": label}
        measures = {
            "user_turns": len(present),
            "said_matched": len(alignment.matched),
            "said_ambiguous": len(alignment.ambiguous),
            "stand_in_results": stand_ins,
            "refused_calls": refused,
        }
        verdicts.append(
            verdict(
                "M2",
                instance,
                f"{label} shows the conversation that happened"
                if not findings
                else f"{label} does not show the conversation that happened",
                events=used,
                findings=findings,
                unobservable="; ".join(unknown) if unknown else None,
                measures=measures,
            )
        )
    return result("M2", policy, verdicts)
