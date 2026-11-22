# Report

`earful report` draws one ledger as one HTML page: a timeline of the run, the checker's
verdicts, and every event.

```bash
earful report run.ledger.jsonl -o report.html
earful report tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule -o report.html
```

```python
from pathlib import Path

from earful.core.html import render, render_bundle
from earful.core.ledger import read_jsonl

page = render(read_jsonl(Path("run.ledger.jsonl")))        # one ledger
page = render_bundle(Path("tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule"))
Path("report.html").write_text(page, encoding="utf-8")
```

`render` calls `check` itself, so the verdicts on the page are the checker's
(`docs/contracts.md`). `render_bundle` draws the reduced ledger of a bundle
(`docs/search.md`, section 6), or the ledger as it was found with `which="original"`,
and adds what the bundle froze. A test that fails with the `earful` fixture writes the
page for each run it made (`docs/testing.md`, section 8).

The page is a single file. It loads no script, font, image or stylesheet from anywhere, so
it opens offline and can be attached to an issue. The same ledger gives the same bytes.
Every string in a ledger is escaped before it reaches the page.

## The header

The case, the seed, the framework and its release, the clock, the run id and the ledger
hash, as `docs/ledger.md` defines them. Then the result of the run, in words:

| Result | Meaning |
| --- | --- |
| Failed | At least one contract has a failed verdict |
| Unobservable | No contract failed, and at least one could not be decided |
| Passed | Every contract that had an instance passed |
| Not exercised | No contract had an instance in this run |
| Invalid run | The run is not valid evidence. The reasons are listed and there are no verdicts |
| Not checked | The header declares a contract policy the checker refuses |

The contracts that had no instance are named beside the result. A contract that was not
exercised did not pass.

## The timeline

Time runs left to right in seconds from the user origin (`user.origin`). The header states
the origin's offset in virtual time. A ledger with no origin is drawn in virtual time, and
the page says so. The scale is 120 pixels per second, and the timeline scrolls sideways.
A run longer than 80 seconds is drawn at a smaller scale, which the page states.

Every event that has a time has a mark. A mark is a link: it highlights the event and
shows its data in the bar above the timeline. An arrow points at the instant something
starts. A pole stands at the instant of a single event.

### 1. User (VAD / STT)

- **speech, as scripted**: each scripted speech as a bar from its true start to its true
  end. These times come from the scenario in `run.start`, with the origin added. Noise, a
  backchannel and a speech that retires an intent are labelled.
- **VAD reports**: `user.vad_start` and `user.vad_end`, where the scripted VAD told the
  framework.
- **final transcript**: `user.transcript_final`, with its text.
- **failure window**, for a bundle's reduced ledger only: how far each speech can start
  while the rule still breaks, from the bundle's `windows`.

A hangup (`user.hangup`) is a dashed line across every lane. A hangup the scenario
declares that the ledger never recorded is drawn in grey and labelled as that.

### 2. Model (LLM)

One bar per model call. It starts at `llm.request`. The hatched part is the wait for the
first token (`ttft_us`), the tick is the first token, and the solid part ends when
generation ends (`duration_us`). Beside it: the reply text, the tool calls the model made
and the claims the reply carries. A call with no `llm.response` is labelled as that.

### 3. Tools / backend

- **tool calls**: one bar per invocation from `tool.called` to its outcome. A filled
  square is a return, a cross is a raise, and a hollow square on a dashed bar is a
  cancellation. A call with no outcome is a dashed bar to the end of the ledger. A tool
  listed in the header's `effects` is labelled `[body replaced by one write]`.
- **backend writes**: one bar per write from `effect.requested` to its outcome. A filled
  diamond is a commit, a hollow diamond is a deduplication, and a cross is a failure. A
  write with no outcome is a dashed bar to the end of the ledger. `effect.timed_out` is
  its own mark with a clock: it says what the caller was told, and the write can still
  commit after it.

A tool returning and a write committing are separate events on separate rows.

### 4. Voice / sink (playout)

- **synthesis**: one mark per `tts.synthesized`. It starts at `started_us`, when the voice
  accepted its first complete word. The hatched part is the wait for the first audio
  (`ttfb_us`) and the tick is its release. Read against the model lane, it shows audio
  that begins before generation ends.
- **playout, word by word**: one bar per segment from `playout.started` to
  `playout.finished`. Each played word is a box whose right edge is its completion time
  (`words_completed_us`). The word the playout stopped inside (`word_cut`) is hatched, with
  how many of its samples played. Words that were synthesized and never played are listed
  under the bar, struck through. Hatching across the bar is time the segment was paused.
  An interrupted segment has a dashed outline and a torn end. A segment cleared before it
  became audible is a dashed box. A word that completed after a hangup is drawn dashed:
  the sink consumed it and no one was there.
- **sink commands**: `sink.pause`, `sink.resume`, `sink.clear` and `sink.flush`, as the
  sink received them. The row above shows what the sink then did.

### 5. Framework

The framework's own `fw.*` events, one row per kind, and the harness's events. They
explain a run and decide no verdict. Marks of one kind that share an instant are drawn
side by side, in ledger order.

### 6. Verdicts

One row per contract, with its result. Each verdict is a mark at the latest event it
rests on. A pass is a circle with a tick, a failure is a square with a cross, an
unobservable verdict is a dashed circle with a question mark, and a contract that was not
exercised is a dotted circle with a dash. The word is always beside the shape.

## Verdicts in full

Below the timeline, each contract has its result, its policy values and its notes. Every
verdict that is not a plain pass is listed with its summary, its findings (the rule and
the detail), its flags and what it measured. Plain passes are folded away under each
contract. A flag such as `reading_split` is shown with what it means.

Each verdict and each finding links to the events it rests on. Following the link
highlights those events on the timeline and in the event table, draws a line through
every lane at each, and names them in the bar above the timeline. This works without
JavaScript. With JavaScript the timeline also scrolls sideways to the first of them, a
mark shows its data under the pointer, and the event table can be filtered.

## Events

Every line of the ledger in order: `seq`, time from the origin, source, kind and data.
Each row has a stable anchor, `e` followed by its position in the ledger, which is its
`seq` in a ledger that validates.

## An invalid run

A ledger that does not validate, or a run that is not valid evidence, is still drawn. The
reasons are at the top of the page and there are no verdicts: an invalid run did not pass
and did not fail. An event that no lane can place is drawn in a row of its own, and one
with no time is listed in the event table only. A run on the real clock is drawn this
way: it is never evidence, and its page is for reading beside the virtual run's.

## What the report does not claim

- Playout is the audio a simulated sink consumed, counted in samples. The report says
  nothing about a network, a client buffer or what a caller heard.
- The user, the model and the voice are scripted providers. The backend is a fake. The
  report describes one run against them.
- A verdict is a statement about that run against the declared contract. Section 7 of
  `docs/contracts.md` says how to decide whether a failure is a defect of the framework.
- The framework lane is explanation. Nothing in it decides a verdict.
- A mark's position is the event's `t_us`. Events that share an instant are ordered by
  `seq`, which the event table shows.
- The page is tested in Chromium only.
