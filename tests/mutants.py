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
        "        return Outcome(case, refused=str(exc))\n",
        "        return Outcome(case)\n",
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
            '        return Outcome(case, error=f"{type(exc).__name__}: {exc}")\n'
        ),
        ("    except KeyboardInterrupt as exc:\n        return Outcome(case, error=str(exc))\n"),
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
        '            move_speeches(redrawn, produced, f"profile {number}")\n',
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
        '        elif e["t_us"] != origins[0]["t_us"] + scenario["hangup_us"]:\n',
        "        elif False:\n",
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
        '        stays = produced if on["hangup_us"] is None else ledger(cases.without_hangup(on))\n',
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
    ),
    Mutant(
        "adapter: a case's hangup is not described",
        "src/earful/livekit/scenario.py",
        (
            '        "hangup_us": None if case.hangup is None else to_us(case.hangup),\n'
            '        "delays": ['
        ),
        ('        "hangup_us": None,\n        "delays": ['),
    ),
    Mutant(
        "harness: a close that raises is swallowed",
        "src/earful/livekit/harness.py",
        "        closing.result()  # a close that raises is the run's error, as at the end of a scenario\n",
        "        pass\n",
    ),
]
