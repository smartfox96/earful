"""M2. Turn integrity: each model call is shown the conversation that happened: every user
turn, every reply and every completed tool call, once each, in order."""

from __future__ import annotations

from . import policy as policies
from .memory import align, user_turns
from .report import ContractResult, Finding, result, verdict
from .view import View


def check(view: View, policy: policies.M2) -> ContractResult:
    verdicts = []
    by_call_id = {t.call_id: t for t in view.tools.values() if t.call_id is not None}
    # The model call that asked for each tool call, and the user turns each call was shown.
    requested_by = {tc["call_id"]: c.call for c in view.calls.values() for tc in c.tool_calls}
    turns_in: dict[int, set[int]] = {}  # model call -> speeches its context held

    for label, event, items in view.contexts:
        findings, unknown, used = [], [], [event]
        delivered = [s for s in view.delivered if s.final["seq"] < event["seq"]]

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

        # Rule b: a delivered transcript is not lost. It is owed a place once the user's
        # turn can be over; until then the framework may still be holding it, and a call
        # made meanwhile for an earlier turn need not show it.
        for speech in delivered:
            if speech.index in present or speech.backchannel:
                continue
            used.append(speech.final)
            late = event["t_us"] - view.turn_over_us(speech)
            if late > policy.user_commit_lag_us:
                findings.append(
                    Finding(
                        "rule b",
                        f"the turn of speech {speech.index} was over {late} us before "
                        "this context, which does not hold its transcript",
                        (speech.final["seq"], event["seq"]),
                    )
                )

        # Rule c: the assistant messages are what the agent was scripted to say (the
        # model's replies and text the application spoke), one to one and in order.
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
            returned = done is not None and done["kind"] == "tool.returned"
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
                continue  # it had not returned and nothing is shown for it: nothing to judge
            asked_in = requested_by.get(tool.call_id)
            if (
                returned
                and done["t_us"] == event["t_us"]
                and asked_in in turns_in
                and present - turns_in[asked_in]
            ):
                # Requested at the very instant the tool returned, for a later user turn
                # than the one the tool was called in: the framework was answering that
                # turn as the result arrived, and cannot be held to an order nothing in
                # the scenario decides. The call that follows up the tool's own turn is.
                returned = False
                if not calls and not outputs:
                    continue
            used += [tool.called, done]
            seqs = tuple(e["seq"] for e in (tool.called, done, event) if e is not None)
            name = f"{tool.name} ({tool.call_id})"
            state = "finished" if done is not None else "was called"
            # Only a call that returned is owed its call and its result. For one that raised,
            # was cancelled or is still running, whether and what the model is shown is the
            # framework's choice; it may still not be shown twice or out of order.
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
                # A result shown for a call that is still running, or was cancelled, stands
                # in for one that does not exist yet. What it says is not judged.
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
            if done["data"]["text"] is None:
                unknown.append(f"the value {name} returned has no stable text form to compare")
            elif shown["output"] != done["data"]["text"]:
                findings.append(
                    Finding(
                        "rule d",
                        f"{name} returned {done['data']['text']!r}, but the context "
                        f"shows {shown['output']!r}",
                        seqs,
                    )
                )
        for index, item in enumerate(items):
            if item["type"] != "function_call_output":
                continue
            tool = by_call_id.get(item["call_id"])
            if tool is not None and tool.called["seq"] < event["seq"]:
                continue  # a witnessed call that had been made: judged above
            if tool is None and item["is_error"]:
                # No tool received this call, and the model is told it failed: the framework
                # answered the call itself (arguments the tool cannot take, a tool that does
                # not exist). That is its account of a call that never ran, and is not judged.
                refused += 1
            elif tool is None and item.get("name") not in view.witnessed_tools:
                unknown.append(
                    f"the tool behind result {item['call_id']!r} was not witnessed, or cannot be linked to it"
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
                source.items, [s for s in view.delivered if s.final["seq"] < source.request["seq"]]
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
