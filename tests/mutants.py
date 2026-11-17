"""Deliberate breaks of the code, one place each. ``scripts/mutate.py`` applies each in a
copy of the repository and checks that a test fails.

A mutant is a piece of source text and what replaces it. The text must occur exactly once
in its file: ``scripts/mutate.py --verify`` checks that, and the lint job runs it, so a
change to the code that moves a mutant's text fails until the mutant is brought along.
"""

# ruff: noqa: E501  (each string is source text, copied exactly)

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mutant:
    name: str  # what is broken, as "<part>: <the break>"
    path: str  # the file, from the repository root
    old: str  # the text as it is: found exactly once in the file
    new: str  # the text that breaks it
    first: str | None = None  # a pytest -k expression: tests to try before the whole suite


MUTANTS = [
    # The checker: ties, stand-ins, refusals and readings.
    Mutant(
        "M1: a stop as the audio ends is held to one reading",
        "src/earful/core/checker/m1.py",
        "        and not either_mark\n",
        "",
        first="stop_that_lands",
    ),
    Mutant(
        "M1: rule 4 goes by audio consumed",
        "src/earful/core/checker/m1.py",
        "if playout.first_audible_us is None:",
        "if not playout.consumed:",
        first="stopped_before_one_sample",
    ),
    Mutant(
        "M2: a call made as its tool returns is held to an order",
        "src/earful/core/checker/m2.py",
        "                and present - turns_in[asked_in]\n",
        "                and False\n",
        first="later_turn_made_as_the_tool_returns or returns_as_the_next_turn",
    ),
    Mutant(
        "M2: a stand-in result is judged only once the tool is done",
        "src/earful/core/checker/m2.py",
        (
            '            if invocation is not None and invocation.called["seq"] < event["seq"]:\n'
            "                continue  # a witnessed call that had been made: judged above\n"
        ),
        (
            '            if invocation is not None and invocation.outcome is not None and invocation.outcome["seq"] < event["seq"] and invocation.outcome["kind"] != "tool.cancelled":\n'
            "                continue\n"
        ),
        first="still_running or slow_tool_interrupted",
    ),
    Mutant(
        "M2: a call the framework refused is judged as a tool's result",
        "src/earful/core/checker/m2.py",
        'if invocation is None and item["is_error"]:',
        "if False:",
        first="no_tool_received or framework_refuses",
    ),
    Mutant(
        "M2: rule b owes the transcript the moment the speech stops",
        "src/earful/core/checker/view.py",
        "        over = stopped(speech) + self.end_of_turn_us\n",
        "        over = stopped(speech)\n",
        first="owed_once_the_turn or extends_it",
    ),
    Mutant(
        "M2: rule b ignores a later speech that extends the turn",
        "src/earful/core/checker/view.py",
        "            over = max(over, stopped(later) + self.end_of_turn_us)\n",
        "            pass\n",
        first="extends_it",
    ),
    Mutant(
        "L1: a reply generated again is not followed",
        "src/earful/core/checker/l1.py",
        "        if call.words and call.call not in walked:\n",
        "        if False:\n",
        first="generated_again_after_a_backchannel",
    ),
    Mutant(
        "L1: a refused tool call is not a step",
        "src/earful/core/checker/l1.py",
        'refused = [r for r in (view.refusal(tc["call_id"]) for tc in rest) if r is not None]',
        "refused = []",
        first="framework_refuses",
    ),
    Mutant(
        "view: the header's witnessed tools are ignored",
        "src/earful/core/checker/view.py",
        'return (set(self.scenario["witnessed_tools"]) | seen) - unlinked',
        "return seen - unlinked",
        first="witnessed_tool_is_never_called or dropped_because_the_user_went_on",
    ),
    Mutant(
        "checker: M2 writes to the ledger it reads",
        "src/earful/core/checker/m2.py",
        '            turns_in[event["data"]["call"]] = present\n',
        (
            '            turns_in[event["data"]["call"]] = present\n'
            '            event["data"]["x"] = 1\n'
        ),
        first="never_changes_the_ledger",
    ),
    Mutant(
        "readings: only the first is checked",
        "src/earful/core/checker/__init__.py",
        "    for reading in possible:\n",
        "    for reading in possible[:1]:\n",
        first="fails_least",
    ),
    Mutant(
        "readings: none are offered",
        "src/earful/core/checker/view.py",
        "    return found if 0 < len(found) <= MAX_READINGS else [{}]\n",
        "    return [{}]\n",
        first="generated_twice or generates_twice or reading_gives",
    ),
    Mutant(
        "readings: the worst is reported",
        "src/earful/core/checker/__init__.py",
        "        if best is None or score < best[0]:\n",
        "        if best is None or score > best[0]:\n",
        first="fails_least",
    ),
    Mutant(
        "memory: alignment takes the first fit",
        "src/earful/core/checker/memory.py",
        "                best[i][j] = min(options, key=lambda option: (option[0], not option[1]))\n",
        "                best[i][j] = options[0]\n",
        first="kindest",
    ),
    Mutant(
        "readings: one reply may answer for two texts",
        "src/earful/core/checker/view.py",
        "            if call not in used:\n",
        "            if True:\n",
        first="reading_gives",
    ),
    # The search, the shrinker, the bundle and what they rest on.
    Mutant(
        "search: a moved speech is placed by the wrong anchor",
        "src/earful/core/search.py",
        '    out["vad_end"] = length + vad["min_silence_us"]\n',
        '    out["vad_end"] = length\n',
    ),
    Mutant(
        "search: a speech too short to interrupt is moved",
        "src/earful/core/search.py",
        '    if length >= session["min_interruption_duration_us"]:\n',
        "    if True:\n",
    ),
    Mutant(
        "search: derived transitions are dropped",
        "src/earful/core/search.py",
        '            mark(t + d["ttft_us"], "llm.first_token")\n',
        "            pass\n",
    ),
    Mutant(
        "search: a refused case counts as run",
        "src/earful/core/search.py",
        "        return Outcome(case, refused=str(exc)), None\n",
        "        return Outcome(case), None\n",
    ),
    Mutant(
        "shrink: ddmin stops early",
        "src/earful/core/shrink.py",
        "            chunks = min(chunks * 2, len(current))\n",
        "            break\n",
    ),
    Mutant(
        "bundle: a replay accepts any run",
        "src/earful/core/bundle.py",
        "        reproduced=all(bundle.predicate in o.failures for o in outcomes),\n",
        "        reproduced=True,\n",
    ),
    Mutant(
        "bundle: a loaded manifest is not validated",
        "src/earful/core/bundle.py",
        "    errors = validate(manifest)\n",
        "    errors = []\n",
    ),
    Mutant(
        "predicate: the rule is ignored",
        "src/earful/core/predicate.py",
        "        Predicate(verdict.contract, finding.rule)\n",
        "        Predicate(verdict.contract, 'rule')\n",
    ),
    Mutant(
        "search: offsets ignored",
        "src/earful/core/search.py",
        "                to = handle.value_us + transition.t_us + offset - handle.t_us\n",
        "                to = handle.value_us + transition.t_us - handle.t_us\n",
    ),
    Mutant(
        "search: no delay limit",
        "src/earful/core/search.py",
        "                too_long = waits and to > max(delay_limit_us, handle.value_us)\n",
        "                too_long = False\n",
    ),
    Mutant(
        "search: a raising run ends the search",
        "src/earful/core/search.py",
        (
            "    except Exception as exc:  # noqa: BLE001\n"
            '        return Outcome(case, error=f"{type(exc).__name__}: {exc}"), None\n'
        ),
        (
            "    except KeyboardInterrupt as exc:\n"
            "        return Outcome(case, error=str(exc)), None\n"
        ),
    ),
    Mutant(
        "search: runner forgets",
        "src/earful/core/search.py",
        "        todo = {key: case for key, case in zip(keys, wanted) if key not in self._seen}\n",
        "        todo = {key: case for key, case in zip(keys, wanted)}\n",
    ),
    Mutant(
        "search: word ends always",
        "src/earful/core/search.py",
        '        elif kind == "playout.finished" and words:\n',
        '        elif kind == "playout.finished":\n',
    ),
    Mutant(
        "search: speech.end moves the start anchors",
        "src/earful/core/search.py",
        '    "speech.end": ("transcript", "vad_end", "turn_end"),\n',
        '    "speech.end": ("vad_start", "transcript", "vad_end", "turn_end"),\n',
    ),
    Mutant(
        "window: never to the microsecond",
        "src/earful/core/search.py",
        "    if lo == hi and resolution_us > 1:\n",
        "    if False:\n",
    ),
    Mutant(
        "window: every end passes",
        "src/earful/core/search.py",
        '                bad, why = probe, "passes" if result is False else "blocked"\n',
        '                bad, why = probe, "passes"\n',
    ),
    Mutant(
        "window: reach ignored",
        "src/earful/core/search.py",
        ('            if step >= reach:\n                return good, "reach"\n'),
        ('            if step >= reach * 1000:\n                return good, "reach"\n'),
    ),
    Mutant(
        "search: everywhere never",
        "src/earful/core/search.py",
        (
            "        return predicate in self.original.failures and all(\n"
            "            predicate in t.outcome.failures for t in ran\n"
            "        )\n"
        ),
        "        return False\n",
    ),
    Mutant(
        "search: coverage ignores why",
        "src/earful/core/search.py",
        ("                if why is None:\n                    reached[name].add(kind)\n"),
        ("                if True:\n                    reached[name].add(kind)\n"),
    ),
    Mutant(
        "case: speeches may pass each other",
        "src/earful/core/case.py",
        ('        speech["stt_delay_us"] = us\n    return None if validate(out) else out\n'),
        (
            '        speech["stt_delay_us"] = us\n'
            '    out["speeches"].sort(key=lambda s: s["start_us"])\n'
            "    return None if validate(out) else out\n"
        ),
    ),
    Mutant(
        "case: a delay declared twice",
        "src/earful/core/case.py",
        "    if len(set(declared)) != len(declared):\n",
        "    if False:\n",
    ),
    Mutant(
        "shrink: nothing-at-all not tried",
        "src/earful/core/shrink.py",
        ("    if fails([]):\n        return []\n"),
        "",
    ),
    Mutant(
        "shrink: differences not isolated",
        "src/earful/core/shrink.py",
        "            if len(needed) < len(all_of_them):\n",
        "            if False:\n",
    ),
    Mutant(
        "shrink: nothing removed",
        "src/earful/core/shrink.py",
        "            if len(kept) < len(items):\n",
        "            if False:\n",
    ),
    Mutant(
        "shrink: settles what does not depend",
        "src/earful/core/shrink.py",
        '        depends = "passes" in (found.lo_end, found.hi_end)\n',
        "        depends = True\n",
    ),
    Mutant(
        "shrink: settles on the edge",
        "src/earful/core/shrink.py",
        "        lo, hi, aim = found.lo_us + quarter, found.hi_us - quarter, (found.lo_us + found.hi_us) // 2\n",
        "        lo, hi, aim = found.lo_us, found.hi_us, found.lo_us\n",
    ),
    Mutant(
        "shrink: budget ignored",
        "src/earful/core/shrink.py",
        "        return runner.runs - start >= max_runs\n",
        "        return False\n",
    ),
    Mutant(
        "shrink: nearest is the first",
        "src/earful/core/shrink.py",
        "    return min(found, key=distance)\n",
        "    return found[0]\n",
    ),
    Mutant(
        "bundle: an unsteady case is kept",
        "src/earful/core/bundle.py",
        "    if distinct != 1:\n",
        "    if False:\n",
    ),
    Mutant(
        "bundle: code change never seen",
        "src/earful/core/bundle.py",
        '        changed = fingerprint(bundle.minimal["scenario"]) != recorded\n',
        "        changed = False\n",
    ),
    Mutant(
        "bundle: a written manifest is not validated",
        "src/earful/core/bundle.py",
        (
            "    errors = validate(manifest)\n"
            "    if errors:\n"
            '        raise ValueError(f"{directory / MANIFEST}'
        ),
        ('    errors = []\n    if errors:\n        raise ValueError(f"{directory / MANIFEST}'),
    ),
    Mutant(
        "adapter: settings not checked",
        "src/earful/livekit/scenario.py",
        "        if not ok:\n",
        "        if False:\n",
    ),
    Mutant(
        "adapter: unknown scenario not a ValueError",
        "src/earful/livekit/scenario.py",
        (
            "    except (ImportError, AttributeError) as exc:\n"
            '        raise ValueError(f"no scenario {scenario!r}: {exc}") from exc\n'
        ),
        ("    except (ImportError, AttributeError):\n        raise\n"),
    ),
    Mutant(
        "adapter: fingerprint constant",
        "src/earful/livekit/scenario.py",
        '    return hashlib.sha256(source.encode("utf-8")).hexdigest() if source else ""\n',
        '    return "0" * 64\n',
    ),
    Mutant(
        "harness: declared delays ignored",
        "src/earful/livekit/harness.py",
        "        return {delay.nth: to_us(delay.seconds) for delay in case.delays if delay.of == of}\n",
        "        return {}\n",
    ),
    Mutant(
        "vad: never merges",
        "src/earful/livekit/providers.py",
        "        if segments and speech.start_us - speeches[segments[-1][-1]].end_us < min_silence_us:\n",
        "        if False:\n",
    ),
    Mutant(
        "L2: point from the speech, not the segment",
        "src/earful/core/checker/l2.py",
        "        reached = max(speech.segment_start + minimum, speech.start)\n",
        "        reached = speech.start + minimum\n",
    ),
    Mutant(
        "L1: one rule name",
        "src/earful/core/checker/l1.py",
        ('    if step == AUDIO:\n        return "milestone 3"\n'),
        ('    if step == AUDIO:\n        return "rule"\n'),
    ),
    Mutant(
        "L1: the voice's default ttfb",
        "src/earful/core/checker/l1.py",
        (
            "        return (\n"
            '            said.event["data"]["ttfb_us"] if said is not None else view.providers["tts"]["ttfb_us"]\n'
            "        )\n"
        ),
        '        return view.providers["tts"]["ttfb_us"]\n',
    ),
    Mutant(
        "validator: vad report for any speech",
        "src/earful/core/schema.py",
        (
            '            elif (k == "user.vad_start" and d["speech"] not in opens) or (\n'
            '                k == "user.vad_end" and d["speech"] not in closes\n'
            "            ):\n"
        ),
        "            elif False:\n",
    ),
    Mutant(
        "model: call ids not unique",
        "src/earful/livekit/providers.py",
        '            t.call_id if t.call_id is not None else f"call-{call}-{n}"\n',
        '            t.call_id if t.call_id is not None else "call"\n',
    ),
    # Backend faults.
    Mutant(
        "backend: a timed-out write never commits",
        "src/earful/core/backend.py",
        '        if fault != "backend.drop":\n',
        '        if fault not in ("backend.drop", "backend.timeout"):\n',
    ),
    Mutant(
        "backend: a rejected write commits",
        "src/earful/core/backend.py",
        'rejected=fault == "backend.error"\n',
        "rejected=False\n",
    ),
    Mutant(
        "backend: the caller is told at once",
        "src/earful/core/backend.py",
        "            await asyncio.sleep(to_seconds(give_up_us))\n",
        "            await asyncio.sleep(0)\n",
    ),
    Mutant(
        "backend: a fault applies to every write",
        "src/earful/core/backend.py",
        "fault, give_up_us = self._faults.get(request, (None, None))",
        "fault, give_up_us = next(iter(self._faults.values()), (None, None))",
    ),
    Mutant(
        "L1: a timed-out wait is not declared time",
        "src/earful/core/checker/l1.py",
        '            e.timed_out["data"]["after_us"] if e.timed_out else e.requested["data"]["latency_us"]\n',
        '            e.requested["data"]["latency_us"]\n',
    ),
    Mutant(
        "view: any outcome is a commit",
        "src/earful/core/checker/view.py",
        'return self.outcome if self.outcome and self.outcome["kind"] == "effect.committed" else None',
        "return self.outcome",
    ),
    Mutant(
        "search: no timeout that commits later",
        "src/earful/core/search.py",
        (
            "                    versions = [\n"
            '                        (f"{of} {nth}, commit first", with_one),\n'
            '                        (f"{of} {nth}, commit later", other),\n'
            "                    ]\n"
        ),
        '                    versions = [(f"{of} {nth}, commit first", with_one)]\n',
    ),
    Mutant(
        "search: the wait is not moved",
        "src/earful/core/search.py",
        "        if gave_up is not None:\n",
        "        if False:\n",
    ),
    Mutant(
        "search: a wait may pass the limit",
        "src/earful/core/search.py",
        "                waits = handle.variable.kind in (*cases.DELAY_KINDS, cases.WAIT)\n",
        "                waits = handle.variable.kind in cases.DELAY_KINDS\n",
    ),
    Mutant(
        "search: a paired timeout loses its later commit",
        "src/earful/core/search.py",
        "                if both is not None and later is not None and later != cases.value(case, commit):\n",
        "                if False:\n",
    ),
    Mutant(
        "search: pairs do not move speeches under a fault",
        "src/earful/core/search.py",
        (
            "            if label in under:\n"
            "                move_speeches(version, under[label], label)\n"
        ),
        ("            if False:\n                move_speeches(version, under[label], label)\n"),
    ),
    Mutant(
        "search: profiles are not seeded",
        "src/earful/core/search.py",
        "        rng = random.Random(f\"{case['seed']}:{number}\")\n",
        "        rng = random.Random()\n",
    ),
    Mutant(
        "search: profiles ignore the case's seed",
        "src/earful/core/search.py",
        "        rng = random.Random(f\"{case['seed']}:{number}\")\n",
        '        rng = random.Random(f"{number}")\n',
    ),
    Mutant(
        "search: speeches are not moved under a profile",
        "src/earful/core/search.py",
        '            move_speeches(profile, produced, f"profile {number}")\n',
        "            pass\n",
    ),
    Mutant(
        "search: an unmoved break is given a window",
        "src/earful/core/search.py",
        "        moved = [t for t in self.breaking(predicate) if t.candidate.variable is not None]\n",
        "        moved = list(self.breaking(predicate))\n",
    ),
    Mutant(
        "case: two faults on one write validate",
        "src/earful/core/case.py",
        "    if len(set(faulted)) != len(faulted):\n",
        "    if False:\n",
    ),
    Mutant(
        "case: a timeout needs no wait",
        "src/earful/core/case.py",
        '        if (fault["us"] is None) != (fault["of"] == "backend.error"):\n',
        "        if False:\n",
    ),
    Mutant(
        "case: an error's wait can be moved",
        "src/earful/core/case.py",
        '            (f for f in out["faults"] if f["nth"] == variable.index and f["us"] is not None), None\n',
        '            (f for f in out["faults"] if f["nth"] == variable.index), None\n',
    ),
    Mutant(
        "shrink: a fault is not a difference",
        "src/earful/core/shrink.py",
        "        if ours_faults.get(nth) != theirs_faults.get(nth):\n",
        "        if False:\n",
    ),
    Mutant(
        "shrink: faults are never removed",
        "src/earful/core/shrink.py",
        ('        ("delays", "declared delays"),\n        ("faults", "faults"),\n    ):\n'),
        ('        ("delays", "declared delays"),\n    ):\n'),
    ),
    Mutant(
        "shrink: a moved wait has no window",
        "src/earful/core/shrink.py",
        (
            "    out += [\n"
            '        Variable(cases.WAIT, f["nth"])\n'
            '        for f in case["faults"]\n'
            '        if f["us"] is not None and waits.get(f["nth"]) != f["us"]\n'
            "    ]\n"
        ),
        "    out += []\n",
    ),
    Mutant(
        "validator: a caller may give up twice",
        "src/earful/core/schema.py",
        '            if d["request"] not in effect_requests or d["request"] in gave_up:\n',
        '            if d["request"] not in effect_requests:\n',
    ),
    Mutant(
        "validator: a rejection is not an outcome",
        "src/earful/core/schema.py",
        (
            '        elif k in ("effect.committed", "effect.deduplicated", "effect.failed"):\n'
            '            if d["request"] not in effect_requests or d["request"] in effect_outcomes:\n'
        ),
        (
            '        elif k in ("effect.committed", "effect.deduplicated", "effect.failed"):\n'
            '            if d["request"] not in effect_requests or (k != "effect.failed" and d["request"] in effect_outcomes):\n'
        ),
    ),
    Mutant(
        "harness: faults do not reach the backend",
        "src/earful/livekit/harness.py",
        (
            "        faults={\n"
            "            f.nth: (f.of, None if f.seconds is None else to_us(f.seconds)) for f in case.faults\n"
            "        },\n"
        ),
        "        faults={},\n",
    ),
    Mutant(
        "harness: two faults on one write run",
        "src/earful/livekit/harness.py",
        "            if fault.nth in faulted:\n",
        "            if False:\n",
    ),
    Mutant(
        "adapter: a case's faults are not built",
        "src/earful/livekit/scenario.py",
        (
            "    case.faults = [\n"
            '        Fault(f["of"], f["nth"], None if f["us"] is None else to_seconds(f["us"]))\n'
            '        for f in data["faults"]\n'
            "    ]\n"
        ),
        "    case.faults = []\n",
    ),
    Mutant(
        "bundle: the manifest miscounts faults",
        "src/earful/core/bundle.py",
        '            "faults": len(shrunk.case["faults"]),\n',
        '            "faults": 0,\n',
    ),
    Mutant(
        "search: no timeout that commits first",
        "src/earful/core/search.py",
        (
            "                    versions = [\n"
            '                        (f"{of} {nth}, commit later", with_one),\n'
            '                        (f"{of} {nth}, commit first", other),\n'
            "                    ]\n"
        ),
        '                    versions = [(f"{of} {nth}, commit later", with_one)]\n',
    ),
    Mutant(
        "search: the commit is not moved under a timeout",
        "src/earful/core/search.py",
        "        if label in as_written:\n",
        "        if False:\n",
    ),
    Mutant(
        "search: a slow write is called commit first",
        "src/earful/core/search.py",
        "                if latency < wait_us:\n",
        "                if True:\n",
    ),
    # The hangup.
    Mutant(
        "backend: a write is dropped when its caller is cancelled",
        "src/earful/core/backend.py",
        ("            pending = self._in_flight.pop(request, None)\n            try:\n"),
        (
            "            pending = self._in_flight.pop(request, None)\n"
            "            if outcome.cancelled():\n"
            "                return\n"
            "            try:\n"
        ),
    ),
    Mutant(
        "backend: a rejected write whose caller has gone is not recorded",
        "src/earful/core/backend.py",
        ('        if fault != "backend.drop":\n            self._schedule('),
        ('        if fault not in ("backend.drop", "backend.error"):\n            self._schedule('),
    ),
    Mutant(
        "harness: the session is not closed at the hangup",
        "src/earful/livekit/harness.py",
        "    closing = asyncio.ensure_future(session.aclose())\n",
        "    closing = asyncio.ensure_future(asyncio.sleep(0))\n",
    ),
    Mutant(
        "harness: the header does not state the hangup",
        "src/earful/livekit/harness.py",
        (
            '        "hangup_us": None if case.hangup is None else to_us(case.hangup),\n'
            '        "intents": ['
        ),
        ('        "hangup_us": None,\n        "intents": ['),
    ),
    Mutant(
        "harness: a hangup before a speech is over runs",
        "src/earful/livekit/harness.py",
        (
            "            if late:\n"
            "                raise ValueError(\n"
            '                    f"the user hangs up at'
        ),
        (
            "            if False:\n"
            "                raise ValueError(\n"
            '                    f"the user hangs up at'
        ),
    ),
    Mutant(
        "harness: the history is read before the session has closed",
        "src/earful/livekit/harness.py",
        (
            "    done, _ = await asyncio.wait({closing}, timeout=case.drain_timeout)\n"
            "    if not done:\n"
            "        # Some releases"
        ),
        ("    done = True\n    if not done:\n        # Some releases"),
    ),
    Mutant(
        "validator: a hangup may be recorded twice",
        "src/earful/core/schema.py",
        "    if len(hangups) > 1:\n",
        "    if False:\n",
    ),
    Mutant(
        "validator: an undeclared hangup validates",
        "src/earful/core/schema.py",
        (
            '        if scenario["hangup_us"] is None:\n'
            '            errors.append(f"{where}: the scenario declares no hangup")\n'
            "        elif"
        ),
        ('        if scenario["hangup_us"] is None:\n            pass\n        elif'),
    ),
    Mutant(
        "validator: a hangup may be at any time",
        "src/earful/core/schema.py",
        '        elif header["clock"] == "virtual" and (\n',
        "        elif False and (\n",
    ),
    Mutant(
        "validator: a real-clock hangup is held to its microsecond",
        "src/earful/core/schema.py",
        '        elif header["clock"] == "virtual" and (\n',
        "        elif (\n",
        first="need_not_fall_on_its_declared_microsecond",
    ),
    Mutant(
        "validator: a hangup may come before a speech is over",
        "src/earful/core/schema.py",
        (
            '    if scenario["hangup_us"] is not None and any(\n'
            '        s["end_us"] > scenario["hangup_us"] for s in speeches\n'
            "    ):\n"
        ),
        "    if False:\n",
    ),
    Mutant(
        "view: words finished after the hangup count as played",
        "src/earful/core/checker/view.py",
        "                if left is not None and at > left:\n",
        "                if False:\n",
    ),
    Mutant(
        "view: audio that starts after the hangup is audible",
        "src/earful/core/checker/view.py",
        "            return None if at is None or (left is not None and at >= left) else at\n",
        "            return at\n",
    ),
    Mutant(
        "view: the word under way at the hangup is not cut",
        "src/earful/core/checker/view.py",
        "                playout.cut = 1  # the word under way when the user left is the one they heard cut\n",
        "                pass\n",
    ),
    Mutant(
        "L1: a hangup excuses nothing",
        "src/earful/core/checker/l1.py",
        (
            "            if (\n"
            "                self._left is not None\n"
            "                and self._left < deadline\n"
            "                and (reached is None or reached > self._left)\n"
            "            ):\n"
        ),
        "            if False:\n",
    ),
    Mutant(
        "L1: a hangup excuses a turn that was overdue",
        "src/earful/core/checker/l1.py",
        ("                self._left is not None\n                and self._left < deadline\n"),
        "                self._left is not None\n",
    ),
    Mutant(
        "M1: playing on after the hangup is not flagged",
        "src/earful/core/checker/m1.py",
        '            flags.append("played_on_after_the_hangup")  # judged on what the user heard\n',
        "            pass\n",
    ),
    Mutant(
        "case: a hangup may come before the last speech ends",
        "src/earful/core/case.py",
        '    elif case["hangup_us"] is not None and case["hangup_us"] < last_end:\n',
        "    elif False:\n",
    ),
    Mutant(
        "case: a user who never speaks may hang up",
        "src/earful/core/case.py",
        '    if case["hangup_us"] is not None and last_end is None:\n',
        "    if False:\n",
    ),
    Mutant(
        "case: a moved hangup is not checked",
        "src/earful/core/case.py",
        ('        out["hangup_us"] = us\n        return None if validate(out) else out\n'),
        ('        out["hangup_us"] = us\n        return out\n'),
    ),
    Mutant(
        "search: the hangup is not moved",
        "src/earful/core/search.py",
        ("        if cases.HANGUP not in kinds:\n            return\n"),
        ("        if True:\n            return\n"),
    ),
    Mutant(
        "search: a declared hangup is moved against its own run",
        "src/earful/core/search.py",
        '        stays = produced if on["hangup_us"] is None else runner.ledger(cases.without_hangup(on))\n',
        "        stays = produced\n",
    ),
    Mutant(
        "search: the hangup is not moved under a fault",
        "src/earful/core/search.py",
        (
            "                move_speeches(version, under[label], label)\n"
            "                move_hangup(version, under[label], label)\n"
        ),
        "                move_speeches(version, under[label], label)\n",
    ),
    Mutant(
        "search: the hangup is always moved",
        "src/earful/core/search.py",
        ("        if cases.HANGUP not in kinds:\n            return\n"),
        ("        if False:\n            return\n"),
    ),
    Mutant(
        "shrink: a hangup is not a difference",
        "src/earful/core/shrink.py",
        '    if author["hangup_us"] != found["hangup_us"]:\n',
        "    if False:\n",
    ),
    Mutant(
        "shrink: the hangup is never removed",
        "src/earful/core/shrink.py",
        '    if current["hangup_us"] is not None and not spent() and breaks(cases.without_hangup(current)):\n',
        "    if False:\n",
    ),
    Mutant(
        "shrink: a moved hangup is not settled",
        "src/earful/core/shrink.py",
        (
            '    if case["hangup_us"] is not None and (\n'
            '        author is None or author["hangup_us"] != case["hangup_us"]\n'
            "    ):\n"
        ),
        "    if False:\n",
    ),
    Mutant(
        "shrink: a difference in the hangup is not applied",
        "src/earful/core/shrink.py",
        ('        elif target[0] == "hangup":\n            out["hangup_us"] = to\n'),
        ('        elif target[0] == "hangup":\n            pass\n'),
    ),
    Mutant(
        "adapter: a case's hangup is not built",
        "src/earful/livekit/scenario.py",
        '    case.hangup = None if data["hangup_us"] is None else to_seconds(data["hangup_us"])\n',
        "    case.hangup = None\n",
        first="a_hangup_is_part_of_a_cases_data",
    ),
    Mutant(
        "adapter: a case's hangup is not described",
        "src/earful/livekit/scenario.py",
        (
            '        "hangup_us": None if case.hangup is None else to_us(case.hangup),\n'
            '        "delays": ['
        ),
        ('        "hangup_us": None,\n        "delays": ['),
        first="a_hangup_is_part_of_a_cases_data",
    ),
    Mutant(
        "harness: a close that raises is swallowed",
        "src/earful/livekit/harness.py",
        "        closing.result()  # a close that raises is the run's error, as at the end of a scenario\n",
        "        pass\n",
        first="a_close_that_raises_at_the_hangup",
    ),
    # The runner's ledgers and the clock patch.
    Mutant(
        "search: a case run for its ledger is run again for its verdict",
        "src/earful/core/search.py",
        "            self._seen.setdefault(key, outcome)\n",
        "            pass\n",
        first="runs_no_case_twice",
    ),
    Mutant(
        "search: a ledger asked for again is run again",
        "src/earful/core/search.py",
        "        todo = {key: case for key, case in zip(keys, wanted) if key not in self._ledgers}\n",
        "        todo = {key: case for key, case in zip(keys, wanted)}\n",
        first="runs_no_case_twice",
    ),
    Mutant(
        "clock: only the first run looks for models",
        "src/earful/livekit/clock.py",
        "    for model in _models():\n",
        "    for model in [] if _LOOKED_AT else _models():\n",
        first="defined_after_the_first_run",
    ),
    Mutant(
        "clock: a model that cannot be patched is not reported",
        "src/earful/livekit/clock.py",
        "                unpatched += names\n",
        "                pass\n",
        first="cannot_be_patched",
    ),
    Mutant(
        "harness: a run with an unpatched clock is valid",
        "src/earful/livekit/harness.py",
        "    if virtual:\n        harness_errors += [\n",
        "    if False:\n        harness_errors += [\n",
        first="cannot_be_patched",
    ),
    Mutant(
        "vad: declares an interval it does not report at",
        "src/earful/livekit/providers.py",
        "        capabilities = lk_vad.VADCapabilities(update_interval=min_speech_duration)\n",
        "        capabilities = lk_vad.VADCapabilities(update_interval=0.1)\n",
        first="declares_the_interval",
    ),
    Mutant(
        "readings: a text may be a reply with no token out yet",
        "src/earful/core/checker/view.py",
        'if c not in taken and first_token[c] <= e["data"]["started_us"]',
        "if c not in taken",
        first="first_token_came or causally_possible or stays_unattributed",
    ),
    Mutant(
        "readings: a first token out as the text is synthesized is too late",
        "src/earful/core/checker/view.py",
        'first_token[c] <= e["data"]["started_us"]',
        'first_token[c] < e["data"]["started_us"]',
        first="first_token_came or causally_possible",
    ),
    Mutant(
        "reading_split: readings that agree are marked",
        "src/earful/core/checker/report.py",
        "_SEVERITY[other.outcome] > _SEVERITY[v.outcome]",
        "_SEVERITY[other.outcome] >= _SEVERITY[v.outcome]",
        first="makes_worse or split_is",
    ),
    Mutant(
        "model: a reply is named before its first token is out",
        "src/earful/livekit/providers.py",
        (
            "        t0 = now_us()\n"
            "        await _sleep_until(t0, ttft_us)\n"
            "        # A text can be this reply only from its first token on.\n"
            "        if words:\n"
            "            model.register.replies[call] = words\n"
        ),
        (
            "        t0 = now_us()\n"
            "        if words:\n"
            "            model.register.replies[call] = words\n"
            "        await _sleep_until(t0, ttft_us)\n"
        ),
        first="names_no_reply",
    ),
    Mutant(
        "reading_split: an instance only another reading judges leaves no note",
        "src/earful/core/checker/report.py",
        "                if other.outcome == PASS or other.instance in judged:\n",
        "                if True:\n",
        first="only_another_reading_judges",
    ),
    # Streaming delivery, immutable final metadata, and gaps between word frames.
    Mutant(
        "streaming: all model chunks arrive at the first token",
        "src/earful/livekit/providers.py",
        "            await _sleep_until(t0, at_us)\n",
        "            await _sleep_until(t0, ttft_us)\n",
        first="model_chunks_are_paced",
    ),
    Mutant(
        "streaming: complete words wait for the end of input",
        "src/earful/livekit/providers.py",
        "                    await push(words)\n",
        "                    pass\n",
        first="voice_emits_complete_words",
    ),
    Mutant(
        "streaming: first audio ignores the voice TTFB",
        "src/earful/livekit/providers.py",
        "                    await _sleep_until(synthesis.t0_us, synthesis.ttfb_us)\n",
        "                    await _sleep_until(synthesis.t0_us, 0)\n",
        first="voice_emits_complete_words",
    ),
    Mutant(
        "streaming: cancellation loses the utterance metadata",
        "src/earful/livekit/providers.py",
        "            if synthesis is not None:\n                synthesis.finish()\n",
        "            if False:\n                synthesis.finish()\n",
        first="cancelled_voice_records_one_prefix",
    ),
    Mutant(
        "streaming: attribution uses finalization instead of synthesis start",
        "src/earful/core/checker/view.py",
        'first_token[c] <= e["data"]["started_us"]',
        'first_token[c] <= e["t_us"]',
        first="first_token_came",
    ),
    Mutant(
        "streaming: an empty buffer keeps playing",
        "src/earful/livekit/sink.py",
        "            segment.starved = True\n            self._pause(segment)\n",
        "            segment.starved = True\n",
        first="streaming_gaps_pause_playout",
    ),
    Mutant(
        "streaming: a zero gap starts audio again",
        "src/earful/core/checker/l2.py",
        ' and e["seq"] not in continuous',
        "",
        first="zero_length_buffer_gap_does_not_start",
    ),
    Mutant(
        "streaming: a zero gap hides a late stop",
        "src/earful/core/checker/view.py",
        '            if resumed is not None and resumed["t_us"] == paused["t_us"]:\n',
        "            if False:\n",
        first="zero_length_buffer_gap_does_not_hide",
    ),
    # A stretched delay, paired with a speech or the hangup.
    Mutant(
        "search: speeches are not moved under a stretched delay",
        "src/earful/core/search.py",
        "                move_speeches(longer, produced, label, coarse=True)\n",
        "                pass\n",
        first="slow_write_and_a_speech",
    ),
    Mutant(
        "search: the hangup is not moved under a stretched delay",
        "src/earful/core/search.py",
        "                move_hangup(longer, produced, label, coarse=True)\n",
        "                pass\n",
        first="slow_write_and_a_hangup",
    ),
    Mutant(
        "search: a delay already as long as the stretch is paired",
        "src/earful/core/search.py",
        "            if delay.value_us < to and longer is not None:\n",
        "            if longer is not None:\n",
        first="shorter_than_the_stretch",
    ),
    Mutant(
        "search: under a stretched delay every offset and word end is used",
        "src/earful/core/search.py",
        "        tried, cannot = candidates(\n            on,\n",
        "        coarse = False\n        tried, cannot = candidates(\n            on,\n",
        first="at_no_word_end",
    ),
    Mutant(
        "shrink: a delay is settled beyond the longest a search makes",
        "src/earful/core/shrink.py",
        "        inside = _up_to(found, max(delay_limit_us, at)) if waits else found\n",
        "        inside = found\n",
        first="no_delay_longer_than_a_search",
    ),
    # Effects: the write a tool stands for, and the backend a run's code finds.
    Mutant(
        "witness: an effect the scenario declares is not made",
        "src/earful/livekit/tools.py",
        "        write = witness.effects.get(called_as, effect)\n",
        "        write = effect\n",
        first="nothing_of_earful",
    ),
    Mutant(
        "witness: a tool is recorded under its function's name",
        "src/earful/livekit/tools.py",
        '        called_as = getattr(call, "name", None) or tool_name\n',
        "        called_as = tool_name\n",
        first="name_the_model_calls_it_by",
    ),
    Mutant(
        "witness: a tool with an effect runs its own body",
        "src/earful/livekit/tools.py",
        "            if write is None or arguments is None:\n",
        "            if True:\n",
        first="nothing_of_earful or declares_its_own_effect",
    ),
    Mutant(
        "witness: the write is made for a call the tool cannot take",
        "src/earful/livekit/tools.py",
        "            if write is None or arguments is None:\n",
        "            if write is None:\n",
        first="cannot_take",
    ),
    Mutant(
        "witness: a tool does not say which effect it declares",
        "src/earful/livekit/tools.py",
        "    setattr(wrapper, _EFFECT, effect)\n",
        "    setattr(wrapper, _EFFECT, None)\n",
        first="declares_its_own_effect",
    ),
    Mutant(
        "harness: an effect for a tool the agent does not have runs",
        "src/earful/livekit/harness.py",
        "    if unknown:\n",
        "    if False:\n",
        first="does_not_have",
    ),
    Mutant(
        "harness: the header states the tool's effect where the scenario declares one too",
        "src/earful/livekit/harness.py",
        "    return {**at_the_tool, **case.effects}\n",
        "    return {**case.effects, **at_the_tool}\n",
        first="another_intent",
    ),
    Mutant(
        "harness: the scenario's effects do not reach the witness",
        "src/earful/livekit/harness.py",
        "    witness = tools.ToolWitness(ledger, case.effects)\n",
        "    witness = tools.ToolWitness(ledger)\n",
        first="nothing_of_earful",
    ),
    Mutant(
        "harness: an effect's tool is not among the tools of its intent",
        "src/earful/livekit/harness.py",
        "        if tool not in known.tools:\n",
        "        if False:\n",
        first="names_the_tools_whose_body or gains_the_tool",
    ),
    Mutant(
        "harness: the header does not state the effects",
        "src/earful/livekit/harness.py",
        "    if effects:\n",
        "    if False:\n",
        first="names_the_tools_whose_body",
    ),
    Mutant(
        "backend: a write outside a run is not refused",
        "src/earful/core/backend.py",
        "    if backend is None:\n",
        "    if False:\n",
        first="no_backend_to_write_to",
    ),
    Mutant(
        "effect: the payload is not mapped",
        "src/earful/core/backend.py",
        "        payload = json_value(arguments) if self.payload is None else self.payload(arguments)\n",
        "        payload = json_value(arguments)\n",
        first="what_the_effect_maps_them_to",
    ),
    Mutant(
        "effect: no key is sent",
        "src/earful/core/backend.py",
        "        key = self.key(arguments) if callable(self.key) else self.key\n",
        "        key = None\n",
        first="what_the_effect_maps_them_to or one_key_serves",
    ),
    Mutant(
        "effect: a key that is one string is not sent",
        "src/earful/core/backend.py",
        "        key = self.key(arguments) if callable(self.key) else self.key\n",
        "        key = self.key(arguments) if callable(self.key) else None\n",
        first="one_key_serves",
    ),
    Mutant(
        "effect: its latency is not the write's",
        "src/earful/core/backend.py",
        "        receipt = await effect(self.intent, payload, key=key, latency=self.latency)\n",
        "        receipt = await effect(self.intent, payload, key=key)\n",
        first="declared_latency",
    ),
    Mutant(
        "effect: the result is not mapped",
        "src/earful/core/backend.py",
        "        return receipt if self.result is None else self.result(receipt, arguments)\n",
        "        return receipt\n",
        first="nothing_of_earful or what_the_effect_maps_them_to",
    ),
    Mutant(
        "validator: a tool may have two effects",
        "src/earful/core/schema.py",
        "    if len(set(replaced)) != len(replaced):\n",
        "    if False:\n",
        first="do_not_fit_its_tools",
    ),
    Mutant(
        "validator: an effect may name a tool that is not witnessed",
        "src/earful/core/schema.py",
        '        if tool not in scenario["witnessed_tools"]:\n',
        "        if False:\n",
        first="do_not_fit_its_tools",
    ),
    Mutant(
        "validator: an effect may name an undeclared intent",
        "src/earful/core/schema.py",
        "        if carried_out_by is None:\n",
        "        if False:\n",
        first="do_not_fit_its_tools",
    ),
    Mutant(
        "validator: an effect's tool need not be among the tools of its intent",
        "src/earful/core/schema.py",
        "        elif tool not in carried_out_by:\n",
        "        elif False:\n",
        first="do_not_fit_its_tools",
    ),
    # The scripted model written as rules.
    Mutant(
        "playbook: the first rule answers whatever the context",
        "src/earful/livekit/playbook.py",
        "            if rule.when(context):\n",
        "            if True:\n",
        first="first_rule_that_holds",
    ),
    Mutant(
        "playbook: user_said reads every turn",
        "src/earful/livekit/playbook.py",
        '    return Condition(lambda c: _heard(c.last_user_turn, phrases), f"user_said{phrases!r}")\n',
        "    return Condition(lambda c: _heard(' '.join(c.user_turns), phrases), f\"user_said{phrases!r}\")\n",
        first="reads_the_latest_turn",
    ),
    Mutant(
        "playbook: a phrase is matched only in its own case",
        "src/earful/livekit/playbook.py",
        "    text = text.lower()\n",
        "    pass\n",
        first="reads_the_latest_turn",
    ),
    Mutant(
        "playbook: one failed result among successes is a failure",
        "src/earful/livekit/playbook.py",
        "        lambda c: bool(c.results_of(tool)) and all(r.failed for r in c.results_of(tool)),\n",
        "        lambda c: any(r.failed for r in c.results_of(tool)),\n",
        first="tell_a_result_a_success",
    ),
    Mutant(
        "playbook: a failed result counts as a success",
        "src/earful/livekit/playbook.py",
        "        lambda c: any(not r.failed for r in c.results_of(tool)), f\"tool_succeeded({tool or ''})\"\n",
        "        lambda c: bool(c.results_of(tool)), f\"tool_succeeded({tool or ''})\"\n",
        first="tell_a_result_a_success",
    ),
    Mutant(
        "playbook: the results of one tool are those of every tool",
        "src/earful/livekit/playbook.py",
        "        return tuple(r for r in self.tool_results if tool is None or r.name == tool)\n",
        "        return self.tool_results\n",
        first="tell_a_result_a_success or read_once",
    ),
    Mutant(
        "playbook: a rule after otherwise is accepted",
        "src/earful/livekit/playbook.py",
        '            raise ValueError("a rule added after otherwise() could never be reached")\n',
        "            pass\n",
        first="could_not_be_reached",
    ),
    Mutant(
        "playbook: a tool call's arguments are not written as JSON",
        "src/earful/livekit/playbook.py",
        "        return (ToolCall(call[0], json.dumps(dict(call[1]))),)\n",
        "        return (ToolCall(call[0], str(dict(call[1]))),)\n",
        first="built_from_plain_values",
    ),
    # A scenario without a name, and what a test calls.
    Mutant(
        "adapter: a Case given as a scenario is changed by the runs made from it",
        "src/earful/livekit/scenario.py",
        "            return cls(lambda: _copied(source), None)\n",
        "            return cls(lambda: source, None)\n",
        first="run_from_its_data",
    ),
    Mutant(
        "adapter: the data of an inline case is looked up by name",
        "src/earful/livekit/scenario.py",
        "    if scenario == INLINE:\n",
        "    if False:\n",
        first="cannot_be_run_by_name",
    ),
    Mutant(
        "adapter: an inline scenario passes for an importable one",
        "src/earful/livekit/scenario.py",
        "    if reference is None or reference == INLINE:\n",
        "    if False:\n",
        first="refused_by_worker_processes or refused_as_a_bundle",
    ),
    Mutant(
        "adapter: a scenario that returns no Case is run",
        "src/earful/livekit/scenario.py",
        "    if not isinstance(case, Case):\n",
        "    if False:\n",
        first="not_a_scenario_is_refused",
    ),
    Mutant(
        "api: a search asked to use workers runs in this process",
        "src/earful/livekit/api.py",
        "    if workers == 0:\n",
        "    if True:\n",
        first="function_in_a_module_is_kept",
    ),
    Mutant(
        "api: a rule no case breaks is kept",
        "src/earful/livekit/api.py",
        "    if not breaking:\n",
        "    if False:\n",
        first="function_in_a_module_is_kept",
    ),
    Mutant(
        "api: a case's data is not what is run",
        "src/earful/livekit/api.py",
        '    return run_case(found.build(case), seed=case["seed"])\n',
        '    return run_case(found.make(), seed=case["seed"])\n',
        first="run_from_its_data",
    ),
    Mutant(
        "report: a broken rule does not fail the assertion",
        "src/earful/core/checker/report.py",
        "        if broken:\n",
        "        if False:\n",
        first="says_which_rule_and_why",
    ),
    Mutant(
        "report: a rule that is allowed to break still fails the assertion",
        "src/earful/core/checker/report.py",
        "        broken = [rule for rule in self.broken() if rule not in allowed(allow)]\n",
        "        broken = self.broken()\n",
        first="says_which_rule_and_why",
    ),
    Mutant(
        "report: a run that is not valid evidence passes the assertion",
        "src/earful/core/checker/report.py",
        "        if not self.valid:\n            raise ContractFailure(\n",
        "        if False:\n            raise ContractFailure(\n",
        first="not_a_valid_run_fails_the_assertion",
    ),
    Mutant(
        "search: a break at single instants fails the assertion",
        "src/earful/core/search.py",
        "            if not one.instants_only and str(one.predicate) not in allowed(allow)\n",
        "            if str(one.predicate) not in allowed(allow)\n",
        first="one_microsecond_is_an_instant",
    ),
    Mutant(
        "search: a rule that is allowed to break still fails the assertion",
        "src/earful/core/search.py",
        "            if not one.instants_only and str(one.predicate) not in allowed(allow)\n",
        "            if not one.instants_only\n",
        first="lasts_30_ms or searched_in_the_process",
    ),
    Mutant(
        "search: a break found by adding a fault is called an instant",
        "src/earful/core/search.py",
        "                    and not unmoved\n",
        "",
        first="instants_only_if_nothing_else_reaches_it",
    ),
    # The command line and the pytest plugin.
    Mutant(
        "cli: a run that could not be judged exits 0",
        "src/earful/cli.py",
        "    return NO_VERDICT if any(not report.valid for report in reports) else OK\n",
        "    return OK\n",
        first="exits_3",
    ),
    Mutant(
        "cli: a rule that is allowed to break still fails the command",
        "src/earful/cli.py",
        "    if any(rule not in allowed(allow) for report in reports for rule in report.broken()):\n",
        "    if any(report.broken() for report in reports):\n",
        first="allowed_to_break_does_not_fail_the_command",
    ),
    Mutant(
        "cli: the ledger a run writes is not in its normal form",
        "src/earful/cli.py",
        "        write_jsonl(result.events, args.ledger, normalized=True)\n",
        "        write_jsonl(result.events, args.ledger)\n",
        first="writes_the_ledger_it_printed",
    ),
    Mutant(
        "cli: a kept rule is not measured when windows are left out",
        "src/earful/cli.py",
        "    broken = found.breaks(windows=keep if args.no_windows else True)\n",
        "    broken = found.breaks(windows=not args.no_windows)\n",
        first="keeps_it_and_the_kept_bundle_replays",
    ),
    Mutant(
        "cli: a search that found a break exits 0",
        "src/earful/cli.py",
        "    return BROKEN if counted else OK\n",
        "    return OK\n",
        first="keeps_it_and_the_kept_bundle_replays",
    ),
    Mutant(
        "cli: a replay that did not do what was expected exits 0",
        "src/earful/cli.py",
        '    return OK if result.reproduced == (args.expect == "reproduced") else BROKEN\n',
        "    return OK\n",
        first="still_has_the_defect",
    ),
    Mutant(
        "cli: what the core refuses is raised, not reported",
        "src/earful/cli.py",
        "    except ValueError as exc:  # a scenario, a case or a bundle the core refuses\n",
        "    except KeyError as exc:\n",
        first="does_not_exist_is_refused",
    ),
    Mutant(
        "adapter: any failed import is reported as the framework missing",
        "src/earful/livekit/__init__.py",
        '    if (exc.name or "").split(".")[0] != "livekit":\n',
        "    if True:\n",
        first="refused_with_what_to_install",
    ),
    Mutant(
        "ledger: a ledger file is read only from a Path",
        "src/earful/core/ledger.py",
        '    text = Path(path).read_text(encoding="ascii")\n',
        '    text = path.read_text(encoding="ascii")\n',
        first="by_name_or_by_path",
    ),
    Mutant(
        "plugin: a passing test leaves evidence",
        "src/earful/pytest_plugin.py",
        '    if helper is None or report.when != "call" or not report.failed:\n',
        '    if helper is None or report.when != "call":\n',
        first="leaves_its_ledgers",
    ),
    Mutant(
        "plugin: the workers the command line gave are not used",
        "src/earful/pytest_plugin.py",
        '        options.setdefault("workers", self.workers)\n',
        "        pass\n",
        first="workers_the_command_line_gave",
    ),
    Mutant(
        "plugin: a run that is not valid evidence is not kept",
        "src/earful/pytest_plugin.py",
        "            self.runs.append(exc.result)\n",
        "            pass\n",
        first="not_valid_evidence_is_kept",
    ),
    Mutant(
        "plugin: the evidence does not go where the command line says",
        "src/earful/pytest_plugin.py",
        '    out = Path(config.getoption("--earful-out"))\n',
        '    out = Path(".earful")\n',
        first="where_the_command_line_says",
    ),
    Mutant(
        "api: a search lets the framework's log records through",
        "src/earful/livekit/api.py",
        "    logger.propagate = False\n",
        "    pass\n",
        first="does_not_fill_the_log",
    ),
    Mutant(
        "cli: a scenario beside the user is not found",
        "src/earful/cli.py",
        "    if here not in sys.path:\n",
        "    if False:\n",
        first="directory_the_command_is_run_from",
    ),
    # The HTML report.
    Mutant(
        "html: text from a ledger is not escaped",
        "src/earful/core/html.py",
        "    return html.escape(text, quote=True)\n",
        "    return text\n",
        first="escaped_everywhere",
    ),
    Mutant(
        "html: following a link to a verdict's events highlights nothing",
        "src/earful/core/html.py",
        '        + "{fill:var(--hl-bg);stroke:var(--hl);background:var(--hl-bg);visibility:visible}"\n',
        '        + "{}"\n',
        first="links_to_its_evidence",
    ),
    Mutant(
        "html: a mark and a row do not carry the verdicts that rest on them",
        "src/earful/core/html.py",
        '        return " ".join([f"s{index}", *(g.anchor for g in self.member.get(index, []))])\n',
        '        return f"s{index}"\n',
        first="links_to_its_evidence",
    ),
    Mutant(
        "html: a finding does not link to its events",
        "src/earful/core/html.py",
        '        findings += f"<li><b>{_e(f.rule)}</b>: {_e(f.detail)}{link}</li>"\n',
        '        findings += f"<li><b>{_e(f.rule)}</b>: {_e(f.detail)}</li>"\n',
        first="links_to_its_evidence",
    ),
    Mutant(
        "html: a verdict's flags are not shown",
        "src/earful/core/html.py",
        "        for flag in v.flags\n",
        "        for flag in ()\n",
        first="passing_run_says_so or hangup_is_marked",
    ),
    Mutant(
        "html: an invalid run does not say why",
        "src/earful/core/html.py",
        '    reasons = "".join(f"<li>{_e(reason)}</li>" for reason in report.invalid)\n',
        '    reasons = ""\n',
        first="invalid_run_shows_why",
    ),
    Mutant(
        "html: the word a playout stopped inside is drawn as played",
        "src/earful/core/html.py",
        '"w-cut", "", tip)',
        '"w", "", tip)',
        first="cut_word_are_marked",
    ),
    Mutant(
        "html: an interrupted segment is drawn as one that played out",
        "src/earful/core/html.py",
        '{"seg-cut" if interrupted or done is None else ""}',
        '{""}',
        first="cut_word_are_marked",
    ),
    Mutant(
        "html: the hangup is not drawn across the lanes",
        "src/earful/core/html.py",
        "        out.append(hangup_line)\n",
        "",
        first="hangup_is_marked",
    ),
    Mutant(
        "html: a word completed after the hangup is drawn as played",
        "src/earful/core/html.py",
        '"w-late" if late else "w"',
        '"w"',
        first="hangup_is_marked",
    ),
    Mutant(
        "html: a tool whose body a write replaced is not marked",
        "src/earful/core/html.py",
        "            if name in effects:\n",
        "            if False:\n",
        first="outcomes_are_told_apart",
    ),
    Mutant(
        "html: the timeout of a write's caller is not drawn",
        "src/earful/core/html.py",
        "            if gave_up is not None:\n",
        "            if False:\n",
        first="outcomes_are_told_apart",
    ),
    Mutant(
        "html: an event no lane draws is left off the timeline",
        "src/earful/core/html.py",
        "        self.rest(fw)\n",
        "",
        first="every_reference_ledger_renders",
    ),
    Mutant(
        "html: the page loads a stylesheet from elsewhere",
        "src/earful/core/html.py",
        "*{box-sizing:border-box}\n",
        "@import url(https://fonts.example/x.css);*{box-sizing:border-box}\n",
        first="refers_to_nothing_outside",
    ),
    Mutant(
        "html: a long run is drawn at the full scale",
        "src/earful/core/html.py",
        "        self.scale = PX_PER_S if span * PX_PER_S <= MAX_WIDTH else MAX_WIDTH / span\n",
        "        self.scale = PX_PER_S\n",
        first="long_run_is_drawn",
    ),
    Mutant(
        "html: a time that cannot be right stretches the page",
        "src/earful/core/html.py",
        "(min(max(t - self.run.base, lo), hi) - lo)",
        "(t - self.run.base - lo)",
        first="cannot_be_right",
    ),
    Mutant(
        "html: the order of the page depends on how a process hashes",
        "src/earful/core/html.py",
        "        for index in sorted(self.placed):\n",
        "        for index in sorted(self.placed, key=lambda i: hash(str(i))):\n",
        first="order_a_process_hashes_in",
    ),
    # The comparison of a real-clock run with the virtual run.
    Mutant(
        "crosscheck: an event far from its virtual time agrees",
        "src/earful/core/crosscheck.py",
        "    late = [name for name in timed if abs(real_at[name] - at[name]) > tolerance.time_us]\n",
        "    late = []\n",
        first="further_from_its_virtual_time",
    ),
    Mutant(
        "crosscheck: the start-up of the session counts as drift",
        "src/earful/core/crosscheck.py",
        '    timed = [name for name in both if ours[name]["seq"] >= began]\n',
        "    timed = both\n",
        first="does_the_same_a_little_later",
    ),
    Mutant(
        "crosscheck: two events at one virtual instant must keep their order",
        "src/earful/core/crosscheck.py",
        "        if at[second] - at[first] > tolerance.time_us and (\n",
        "        if at[second] - at[first] >= 0 and (\n",
        first="may_come_in_either_order",
    ),
    Mutant(
        "crosscheck: an event out of order agrees",
        "src/earful/core/crosscheck.py",
        '            theirs[second]["seq"] < theirs[first]["seq"]\n',
        "            False\n",
        first="clearly_follows_is_out_of_order",
    ),
    Mutant(
        "crosscheck: an event one clock lacks is not named",
        "src/earful/core/crosscheck.py",
        "        for name in ours\n        if name not in theirs\n",
        "        for name in ours\n        if False\n",
        first="only_one_clock_has",
    ),
    Mutant(
        "crosscheck: a segment that played out may lack words",
        "src/earful/core/crosscheck.py",
        '        elif abs(a["played"] - b["played"]) > (tolerance.words if a["interrupted"] else 0):\n',
        '        elif abs(a["played"] - b["played"]) > tolerance.words:\n',
        first="played_out_has_to_have_played_every_word",
    ),
    Mutant(
        "crosscheck: the words a cut segment played are not compared",
        "src/earful/core/crosscheck.py",
        '        elif abs(a["played"] - b["played"]) > (tolerance.words if a["interrupted"] else 0):\n',
        "        elif False:\n",
        first="may_differ_by_the_declared_number_of_words",
    ),
    Mutant(
        "crosscheck: a segment cut short on one clock only agrees",
        "src/earful/core/crosscheck.py",
        '        elif (a["interrupted"], a["audible"]) != (b["interrupted"], b["audible"]):\n',
        "        elif False:\n",
        first="cut_short_on_one_clock_only",
    ),
    Mutant(
        "crosscheck: a remembered reply may differ by any number of words",
        "src/earful/core/crosscheck.py",
        "        return long[: len(short)] == short and len(long) - len(short) <= tolerance.words\n",
        "        return long[: len(short)] == short\n",
        first="remembered_shorter_by_the_declared_words",
    ),
    Mutant(
        "crosscheck: what a model call was shown is not compared",
        "src/earful/core/crosscheck.py",
        "            if not _same_item(x, y, tolerance):\n",
        "            if False:\n",
        first="another_item_or_another_user_text",
    ),
    Mutant(
        "crosscheck: the writes are not compared",
        "src/earful/core/crosscheck.py",
        "    if ours == theirs:\n        return []\n",
        "    if True:\n        return []\n",
        first="did_not_commit_on_the_real_clock",
    ),
    Mutant(
        "crosscheck: a real run that breaks other rules agrees",
        "src/earful/core/crosscheck.py",
        "    if rules != real_rules:\n",
        "    if False:\n",
        first="did_not_commit_on_the_real_clock",
    ),
    Mutant(
        "crosscheck: a virtual run is taken for a real one",
        "src/earful/core/crosscheck.py",
        "    if _REAL not in reasons:\n",
        "    if False:\n",
        first="not_a_virtual_and_a_real_run",
    ),
    Mutant(
        "crosscheck: runs of two cases are compared",
        "src/earful/core/crosscheck.py",
        "        if ours[name] != theirs[name]:\n",
        "        if False:\n",
        first="not_a_virtual_and_a_real_run",
    ),
    Mutant(
        "crosscheck: a real run that is not a complete run is compared",
        "src/earful/core/crosscheck.py",
        "    if other:\n        return Comparison((), 0, rules, (), unusable=other)\n",
        "    if False:\n        return Comparison((), 0, rules, (), unusable=other)\n",
        first="not_a_complete_run_for_another_reason",
    ),
    Mutant(
        "crosscheck: a check with no real run agrees",
        "src/earful/core/crosscheck.py",
        "        return bool(self.comparisons) and all(c.agrees for c in self.comparisons)\n",
        "        return all(c.agrees for c in self.comparisons)\n",
        first="how_many_runs_agree",
    ),
    Mutant(
        "cli: a virtual run that is not valid is cross-checked",
        "src/earful/cli.py",
        "        return NO_VERDICT\n    if args.out is not None:\n",
        "        return OK\n    if args.out is not None:\n",
        first="refused_or_has_no_verdict",
    ),
    # What a review found: retired intents, worker processes, copies and a manifest.
    Mutant(
        "harness: a speech cannot retire an intent that only an effect declares",
        "src/earful/livekit/harness.py",
        "    declared = {it.intent for it in intents}\n",
        "    declared = {it.intent for it in case.intents}\n",
        first="retire_an_intent_that_only_an_effect_declares",
    ),
    Mutant(
        "harness: a speech may retire an intent nothing declares",
        "src/earful/livekit/harness.py",
        '            raise ValueError(f"speech at {speech.start} s retires undeclared intents {undeclared}")\n',
        "            pass\n",
        first="retires_an_intent_nothing_declares",
    ),
    Mutant(
        "api: a search starts its workers before it checks what it is asked for",
        "src/earful/livekit/api.py",
        "    variables, faults = search.check_arguments(start, variables, faults)\n",
        "    variables, faults = tuple(variables), tuple(faults)\n",
        first="checked_before_a_worker_is_started or starts_no_worker",
    ),
    Mutant(
        "api: a pool that cannot be made is an unhandled error",
        "src/earful/livekit/api.py",
        "    except (OSError, NotImplementedError, ImportError) as exc:\n",
        "    except ImportError as exc:\n",
        first="cannot_be_started",
    ),
    Mutant(
        "cli: a search told a number of workers falls back all the same",
        "src/earful/cli.py",
        "        if args.workers is not None:  # asked for by number: say so, do not do otherwise\n",
        "        if False:\n",
        first="unless_told_a_number",
    ),
    Mutant(
        "cli: a search does not fall back where workers cannot be started",
        "src/earful/cli.py",
        "        if args.workers is not None:  # asked for by number: say so, do not do otherwise\n",
        "        if True:\n",
        first="unless_told_a_number",
    ),
    Mutant(
        "adapter: a run shares the caller's effects",
        "src/earful/livekit/scenario.py",
        "        effects=dict(case.effects),\n",
        "        effects=case.effects,\n",
        first="keeps_its_lists_and_dicts",
    ),
    Mutant(
        "adapter: a run shares the caller's contract policy",
        "src/earful/livekit/scenario.py",
        "        contracts=copy.deepcopy(case.contracts),\n",
        "        contracts=dict(case.contracts),\n",
        first="keeps_its_lists_and_dicts",
    ),
    Mutant(
        "html: a window that is not one stops the page",
        "src/earful/core/html.py",
        '            if isinstance(w, dict) and w.get("instant") is True\n',
        '            if w.get("instant") is True\n',
        first="windows_are_not_what_they_should_be",
    ),
]
