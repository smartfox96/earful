"""The HTML report: every ledger renders to one self-contained page, the page says what
the checker says, and nothing from a ledger reaches it unescaped.

These tests run no framework. They read the reference ledgers and the reference bundles.
"""

import copy
import hashlib
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from earful.core import html as report
from earful.core.checker import FAIL, NOT_EXERCISED, PASS, UNOBSERVABLE, check
from earful.core.checker.report import ContractResult, Finding, Report, Verdict
from earful.core.ledger import ledger_hash, read_jsonl, run_id

HERE = Path(__file__).parent
GOLDEN = sorted((HERE / "golden").glob("*.jsonl"))
BUNDLES = sorted(p for p in (HERE / "bundles").iterdir() if p.is_dir())
LANES = [
    "User (VAD / STT)",
    "Model (LLM)",
    "Tools / backend",
    "Voice / sink (playout)",
    "Framework",
    "Verdicts",
]
VOID = {"meta", "br", "hr", "img", "input", "link"}


def golden(name):
    return read_jsonl(HERE / "golden" / f"{name}.ledger.normalized.jsonl")


class Doc(HTMLParser):
    """A page, parsed: its tags must balance, and what it holds is indexed for asserting."""

    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.stack = []  # (tag, id, in the legend)
        self.ids = []
        self.hrefs = []
        self.tags = []  # (tag, attributes)
        self.timeline = []  # classes of every element drawn in the timeline itself
        self.within = {}  # id -> the hrefs inside that element
        self.texts = []
        self.feed(page)
        self.close()
        self.errors += [f"<{tag}> is never closed" for tag, _id, _legend in self.stack]
        self.text = " ".join(" ".join(self.texts).split())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        classes = (attrs.get("class") or "").split()
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "href" in attrs:
            self.hrefs.append(attrs["href"])
            for _tag, inside, _legend in self.stack:
                if inside is not None:
                    self.within.setdefault(inside, []).append(attrs["href"])
        legend = "lg" in classes or any(legend for _t, _i, legend in self.stack)
        drawn = any(t == "svg" for t, _i, _l in self.stack)
        if drawn and not legend:
            self.timeline += classes
        if tag not in VOID:
            self.stack.append((tag, attrs.get("id"), legend))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1][0] if self.stack else 'nothing'}")
            return
        self.stack.pop()

    def handle_data(self, data):
        if not self.stack or self.stack[-1][0] not in ("script", "style"):
            self.texts.append(data)

    def count(self, tag):
        return sum(1 for name, _attrs in self.tags if name == tag)


def well_formed(page):
    """The page parsed, after checking that it holds together."""
    doc = Doc(page)
    assert not doc.errors, doc.errors[:5]
    assert len(doc.ids) == len(set(doc.ids)), "an id is used twice"
    missing = {h for h in doc.hrefs if h.startswith("#") and h[1:] not in doc.ids}
    assert not missing, f"links to anchors that do not exist: {sorted(missing)[:5]}"
    return doc


def renumbered(events):
    """``events`` with seq and the run id made right again after an edit."""
    run = run_id(events[0]["data"])
    for seq, event in enumerate(events):
        event["seq"], event["run"] = seq, run
    return events


def with_hangup(events, at_us):
    """A copy of ``events`` in which the user hangs up ``at_us`` after the origin."""
    events = copy.deepcopy(events)
    events[0]["data"]["scenario"]["hangup_us"] = at_us
    t = next(e["t_us"] for e in events if e["kind"] == "user.origin") + at_us
    at = 1 + max(i for i, e in enumerate(events) if e["t_us"] <= t and e["kind"] != "run.end")
    events.insert(
        at, {**events[1], "source": "world", "kind": "user.hangup", "t_us": t, "data": {}}
    )
    return renumbered(events)


def envelope(seq, t_us, source, kind, **data):
    return {
        "schema": "earful.ledger/1",
        "run": "run-0",
        "seq": seq,
        "t_us": t_us,
        "source": source,
        "kind": kind,
        "data": data,
    }


# ------------------------------------------------------------------ every ledger renders


@pytest.mark.parametrize("path", GOLDEN, ids=lambda p: p.name.split(".ledger")[0])
def test_every_reference_ledger_renders_to_a_whole_page(path):
    events = read_jsonl(path)
    page = report.render(events)
    doc = well_formed(page)
    assert len(page.encode("utf-8")) < 400_000
    assert ledger_hash(events) in doc.text
    assert events[0]["run"] in doc.text
    # Every line of the ledger has a row, a bar that describes it, and a mark on the timeline.
    for index in range(len(events)):
        assert f"e{index}" in doc.ids and f"t{index}" in doc.ids
        assert f"s{index}" in doc.timeline, f"event {index} is not on the timeline"


@pytest.mark.parametrize("which", ["minimal", "original"])
@pytest.mark.parametrize("directory", BUNDLES, ids=lambda p: p.name)
def test_both_ledgers_of_each_bundle_render_with_what_the_bundle_froze(directory, which):
    page = report.render_bundle(directory, which)
    doc = well_formed(page)
    contract, rule = directory.name.rsplit(".", 1)[1].split("-", 1)
    assert "From a failure bundle" in doc.text
    assert (
        f"The bundle froze {contract}/{rule.replace('-', ' ')} on livekit-agents 1.5.6" in doc.text
    )
    assert "Reduction." in doc.text and "Windows." in doc.text
    assert "speech.position 0" in doc.text
    assert f"({which}): its hash matches the manifest" in doc.text
    # The windows are drawn only on the reduced case, which they were measured on.
    assert ("window" in doc.timeline) == (which == "minimal")


def test_a_bundle_has_only_the_two_ledgers():
    with pytest.raises(ValueError, match="no 'other' ledger"):
        report.render_bundle(BUNDLES[0], "other")


def test_the_same_events_give_the_same_bytes():
    events = golden("lk-1.5.6.book_then_interrupt")
    first = report.render(events)
    assert report.render(events) == first
    assert report.render(copy.deepcopy(events)) == first
    assert report.render_bundle(BUNDLES[0]) == report.render_bundle(BUNDLES[0])


def test_the_page_does_not_depend_on_the_order_a_process_hashes_in():
    path = HERE / "golden" / "lk-1.5.6.book_then_interrupt.ledger.normalized.jsonl"
    script = (
        "import hashlib, sys; from pathlib import Path; "
        "from earful.core import html; from earful.core.ledger import read_jsonl; "
        "page = html.render(read_jsonl(Path(sys.argv[1]))); "
        "print(hashlib.sha256(page.encode('utf-8')).hexdigest())"
    )
    here = hashlib.sha256(report.render(read_jsonl(path)).encode("utf-8")).hexdigest()
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": os.pathsep.join(sys.path)}
        done = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == here


@pytest.mark.parametrize("path", GOLDEN[:3] + BUNDLES[:1], ids=lambda p: p.name[:40])
def test_the_page_refers_to_nothing_outside_itself(path):
    page = report.render_bundle(path) if path.is_dir() else report.render(read_jsonl(path))
    doc = Doc(page)
    assert "://" not in page
    assert "@import" not in page and "url(http" not in page
    assert doc.count("link") == 0 and doc.count("img") == 0 and doc.count("iframe") == 0
    for _tag, attrs in doc.tags:
        assert "src" not in attrs
        assert attrs.get("href", "#").startswith("#")


def test_each_lane_is_there_in_order():
    page = report.render(golden("lk-1.8.12.book_then_interrupt"))
    doc = well_formed(page)
    at = [doc.text.index(lane) for lane in LANES]
    assert at == sorted(at)
    for drawn in ("speech", "gen", "bar-tool", "bar-write", "wait-voice", "seg", "w", "fw-tick"):
        assert drawn in doc.timeline, drawn
    assert "They decide no verdict." in doc.text
    assert "simulated audio sink" in doc.text and "fake backend" in doc.text
    assert "scripted providers" in doc.text


# ---------------------------------------------------------------------------- escaping

HOSTILE = "<script>alert(\"x\")</script> & 'q' <img src=x onerror=alert(1)>"


def test_text_from_a_ledger_is_escaped_everywhere():
    events = golden("lk-1.5.6.book_then_interrupt")
    events[0]["data"]["case"] = "</title><script>alert(1)</script>"
    events[0]["data"]["scenario"]["speeches"][0]["transcript"] = HOSTILE
    for event in events:
        data = event["data"]
        if event["kind"] == "user.transcript_final" and data["speech"] == 0:
            data["text"] = HOSTILE
        if event["kind"] == "tool.called":
            data["arguments"] = {"party_size": HOSTILE, '"><svg onload=alert(1)>': 1}
            data["name"] = "create'\"><script>alert(2)</script>"
        if event["kind"] == "llm.response":
            data["text"] = HOSTILE + " " + data["text"]
        if event["kind"] == "fw.close":
            event["kind"] = 'fw.x" onmouseover="alert(3)'
            event["source"] = "framework'\"><b>"
    page = report.render(renumbered(events), title='"><script>alert(4)</script>')
    doc = well_formed(page)
    assert doc.count("script") == 1, "only the page's own script"
    assert doc.count("img") == 0
    for tag, attrs in doc.tags:
        assert not any(name.startswith("on") for name in attrs), (tag, attrs)
    # The text is all there, as text.
    assert HOSTILE in doc.text
    assert '"><script>alert(4)</script>' in doc.text
    assert "tool 1 create'\"><script>alert(2)" in doc.text
    assert "<script>alert" not in page and "<img" not in page and "<svg onload" not in page
    # The page's own script holds nothing from the ledger.
    script = page[page.index("<script>") : page.index("</script>")]
    assert "alert" not in script and "party_size" not in script


def test_a_ledger_that_is_not_json_objects_still_renders():
    for events in ([], [{"a": 1}, "x", 3, None], [{"kind": "run.start", "data": "no"}]):
        doc = well_formed(report.render(events))
        assert "Invalid run" in doc.text


def test_a_time_that_cannot_be_right_does_not_stretch_the_page():
    """A corrupt time is drawn at the edge of the axis, and the page stays a page."""
    for field, where in (
        ("t_us", None),
        ("duration_us", "llm.response"),
        ("started_us", "tts.synthesized"),
    ):
        events = golden("lk-1.8.12.barge_in_mid_word")
        if where is None:
            events[20]["t_us"] = 10**15
        else:
            next(e for e in events if e["kind"] == where)["data"][field] = 10**15
        events[0]["data"]["scenario"]["speeches"][1]["end_us"] = 10**15
        page = report.render(events)
        doc = well_formed(page)
        width = next(float(a["width"]) for tag, a in doc.tags if tag == "svg" and "role" in a)
        assert width < report.MAX_WIDTH + 2000, field
        assert len(page) < 400_000


# ---------------------------------------------------------------------------- verdicts


def test_a_failed_verdict_is_shown_with_its_finding_and_links_to_its_evidence():
    events = golden("lk-1.5.6.book_then_interrupt")
    checked = check(events)
    verdicts = [v for result in checked.results.values() for v in result.verdicts]
    failed = next(v for v in verdicts if v.contract == "A2")
    number = 1 + verdicts.index(failed)
    page = report.render(events)
    doc = well_formed(page)

    assert "A2, M2 failed." in doc.text
    assert "A2 One logical effect fail" in doc.text
    assert failed.summary in doc.text
    assert "rule : 2 durable effects for 'book-table-1'; the scenario authorized 1" in doc.text
    # The verdict links to a bar that highlights its events, and to each event.
    inside = doc.within[f"d{number}"]
    assert f"#v{number}" in inside and f"#f{number}-1" in inside
    assert failed.evidence == (17, 18, 44, 45)
    for seq in failed.evidence:
        assert f"#t{seq}" in inside
    # Following the link highlights them on the timeline and in the table, without a script.
    declared = re.search(rf"#v{number}:target~section \.v{number}\b[^}}]*\{{([^}}]*)\}}", page)
    assert declared is not None
    for shown in ("fill:var(--hl-bg)", "background:var(--hl-bg)", "visibility:visible"):
        assert shown in declared.group(1)
    rows = {a.get("id"): a.get("class", "").split() for tag, a in doc.tags if tag == "tr"}
    marks = [
        attrs["class"].split() for tag, attrs in doc.tags if tag == "rect" and "class" in attrs
    ]
    for seq in failed.evidence:
        assert f"v{number}" in rows[f"e{seq}"]
        assert any("hit" in mark and f"s{seq}" in mark and f"v{number}" in mark for mark in marks)
    assert f"v{number}" not in rows["e16"]
    # A contract with no instance is said to be that, and is not a pass.
    assert "A1 Retired intent not exercised" in doc.text
    assert "That is not a pass." in doc.text


def test_a_passing_run_says_so_and_keeps_what_was_not_exercised_apart():
    doc = well_formed(report.render(golden("lk-1.8.12.barge_in_mid_word")))
    assert "M1, M2, L1, L2 passed." in doc.text
    assert "A1, A2, A3 had no instance in this run (not exercised)." in doc.text
    assert "Failed" not in doc.text and "Invalid run" not in doc.text
    assert "st-none" in doc.timeline or "not exercised: this run has no instance of it" in doc.text
    # A pass that carries a flag is listed in full, with what the flag means.
    assert "judged_on_final_history" in doc.text
    assert "the final chat history is the judged record" in doc.text


def split_reading(events):
    """A report, made by hand, with an unobservable verdict that another reading makes
    worse, a note, a plain pass and a contract with no instance."""
    unseen = Verdict(
        contract="M1",
        instance={"reply": 1},
        outcome=UNOBSERVABLE,
        summary="what was played of reply 1 is not known",
        findings=(Finding("unobservable", "the segment never finished", (17,)),),
        evidence=(11, 17),
        witnesses=("llm", "sink"),
        flags=("most_favourable_reading", "reading_split", "a_flag_of_the_future"),
    )
    passed = Verdict("M2", {"context": "call 1"}, PASS, "call 1 shows it", evidence=(10,))
    results = {
        "M1": ContractResult(
            "M1",
            UNOBSERVABLE,
            {"ahead_us": 1},
            (unseen,),
            ("another reading judges reply 2, which this one does not: fail",),
        ),
        "M2": ContractResult("M2", PASS, {}, (passed,)),
        "L2": ContractResult("L2", NOT_EXERCISED, {}),
    }
    return Report(events[0]["run"], True, (), results, reading={2: 2}, readings=2)


def test_unobservable_and_a_split_reading_are_shown():
    events = golden("lk-1.8.12.barge_in_mid_word")
    page = report._page(events, split_reading(events))
    doc = well_formed(page)

    assert "M1 could not be decided (unobservable). M2 passed." in doc.text
    assert "Failed" not in doc.text
    assert "M1 Played vs. remembered unobservable" in doc.text
    assert "unobservable reply 1 : what was played of reply 1 is not known" in doc.text
    assert "unobservable : the segment never finished" in doc.text
    assert "reading_split Another reading of the ledger gives this instance a worse outcome" in (
        doc.text
    )
    assert "a_flag_of_the_future A qualification the checker attached" in doc.text
    assert "Note. another reading judges reply 2, which this one does not: fail" in doc.text
    assert "The ledger allows 2 readings" in doc.text and "utterance 2 as call 2" in doc.text
    # Each outcome has its own shape, on the timeline and in the list.
    assert {"st-unob", "st-pass", "st-none"} <= set(doc.timeline)
    assert "st-fail" not in doc.timeline
    classes = {c for _tag, attrs in doc.tags for c in (attrs.get("class") or "").split()}
    assert {"o-unobservable", "b-unobservable", "b-not_exercised", "b-pass"} <= classes
    assert "#v1" in doc.within["d1"] and "#f1-1" in doc.within["d1"]


def test_an_invalid_run_shows_why_and_has_no_verdicts():
    events = golden("lk-1.5.6.book_then_interrupt")[:40]
    checked = check(events)
    assert not checked.valid
    doc = well_formed(report.render(events))
    assert "Invalid run" in doc.text
    for reason in checked.invalid:
        assert reason in doc.text
    assert "It did not pass and it did not fail." in doc.text
    assert "There are no verdicts." in doc.text
    classes = {c for _tag, attrs in doc.tags for c in (attrs.get("class") or "").split()}
    assert "verdict" not in classes and "chip" not in classes
    assert not {"st-pass", "st-fail", "st-unob"} & set(doc.timeline)
    assert not any(i.startswith(("c-", "d")) and i != "d" for i in doc.ids if i[1:2].isdigit())
    # What the ledger holds is still drawn.
    for lane in LANES:
        assert lane in doc.text
    assert {"speech", "gen", "seg", "s39"} <= set(doc.timeline)
    assert "e39" in doc.ids and "e40" not in doc.ids


def test_a_run_on_the_real_clock_is_invalid_and_says_so():
    events = golden("lk-1.8.12.barge_in_mid_word")
    events[0]["data"]["clock"] = "real"
    doc = well_formed(report.render(renumbered(events)))
    assert "Invalid run" in doc.text and "the run was on the real clock" in doc.text


def test_a_policy_the_checker_refuses_is_reported_and_the_run_is_still_drawn():
    events = golden("lk-1.8.12.barge_in_mid_word")
    events[0]["data"]["contracts"] = {"A2": {"no_such_value": 1}}
    doc = well_formed(report.render(renumbered(events)))
    assert "Not checked" in doc.text and "no_such_value" in doc.text
    assert "seg" in doc.timeline


# ------------------------------------------------------------------------ what is drawn


def test_an_interrupted_segment_and_its_cut_word_are_marked():
    events = golden("lk-1.8.12.barge_in_mid_word")
    done = [e["data"] for e in events if e["kind"] == "playout.finished"]
    assert done[0]["interrupted"] and done[0]["word_cut"] is not None
    assert not done[1]["interrupted"]
    doc = well_formed(report.render(events))
    segments = [
        attrs["class"].split()
        for tag, attrs in doc.tags
        if tag == "rect" and "seg" in (attrs.get("class") or "").split()
    ]
    assert sorted("seg-cut" in classes for classes in segments) == [False, True]
    assert doc.timeline.count("w-cut") == 1 and doc.timeline.count("paused") == 1
    assert doc.timeline.count("w") == len(done[0]["words_played"]) + len(done[1]["words_played"])
    assert "segment 1 interrupted after 2.300 s of 7.440 s of audio" in doc.text
    assert f'{len(done[0]["words_played"])} words played, cut "' in doc.text
    assert f"({done[0]['word_cut_samples']} of " in doc.text
    assert "ghost" in doc.timeline, "the words that never played are shown as not played"
    assert "segment 2 played out, 0.720 s of audio: 2 words played" in doc.text
    # The sink's commands and what playout did are both there.
    for said in ("pause", "clear", "resume", "flush"):
        assert f">{said}</text>" in report.render(events)


def test_synthesis_and_the_model_are_drawn_from_their_own_times():
    events = golden("lk-1.8.12.barge_in_mid_word")
    doc = well_formed(report.render(events))
    assert "first token +0.100 s, end +0.400 s" in doc.text
    assert "utterance 1: reply of call 1 (unique), 29 words" in doc.text
    assert {"wait-model", "tick-model", "wait-voice", "tick-voice"} <= set(doc.timeline)


def test_a_hangup_is_marked_across_the_lanes():
    events = golden("lk-1.8.12.barge_in_mid_word")
    assert "hangup" not in Doc(report.render(events)).timeline
    left = with_hangup(events, 7_200_000)
    assert check(left).valid
    doc = well_formed(report.render(left))
    assert "hangup" in doc.timeline
    assert "user hangs up, 7.200 s" in doc.text
    # Words the sink went on to play reached no one, and are drawn as that.
    assert "w-late" in doc.timeline
    assert "words after the hangup reached no one" in doc.text
    assert "played_on_after_the_hangup" in doc.text
    # Declared and never recorded: the ledger stops short of it.
    cut = [e for e in left if e["kind"] != "user.hangup"]
    doc = well_formed(report.render(cut))
    assert "hangup declared at 7.200 s, not recorded" in doc.text
    assert "hangup-open" in doc.timeline


def test_a_run_with_no_speech_and_no_playout_renders():
    events = golden("lk-1.8.12.barge_in_mid_word")
    bare = [events[0], events[-1]]
    bare[0]["data"]["scenario"]["speeches"] = []
    assert check(renumbered(bare)).valid
    doc = well_formed(report.render(bare))
    assert "the scenario declares no speech" in doc.text
    assert "nothing was played" in doc.text and "no model call" in doc.text
    assert "No user.origin in this ledger." in doc.text
    assert "Not exercised" in doc.text


def every_outcome():
    """A ledger, written by hand, in which a tool raises, returns, is cancelled and never
    finishes, and a write commits late, is deduplicated, fails and has no outcome."""
    header = golden("lk-1.5.6.book_then_interrupt")[0]["data"]
    header["scenario"]["effects"] = [{"tool": "book", "intent": "book-table-1"}]
    return [
        envelope(0, 0, "harness", "run.start", **header),
        envelope(1, 0, "world", "user.origin", by="stt"),
        envelope(
            2, 1_000_000, "world", "tool.called", tool=1, name="book", call_id="a", arguments={}
        ),
        envelope(
            3,
            1_000_000,
            "backend",
            "effect.requested",
            request=1,
            intent="book-table-1",
            key="k",
            payload={},
            tool=1,
            latency_us=500_000,
        ),
        envelope(
            4,
            1_200_000,
            "backend",
            "effect.timed_out",
            request=1,
            intent="book-table-1",
            key="k",
            after_us=200_000,
        ),
        envelope(
            5,
            1_200_000,
            "world",
            "tool.raised",
            tool=1,
            name="book",
            error="TimeoutError",
            message="gave up",
            expects_reply=True,
        ),
        envelope(
            6,
            1_500_000,
            "backend",
            "effect.committed",
            request=1,
            intent="book-table-1",
            key="k",
            receipt="R1",
            count_for_intent=1,
        ),
        envelope(
            7, 2_000_000, "world", "tool.called", tool=2, name="look", call_id="b", arguments={}
        ),
        envelope(
            8,
            2_000_000,
            "backend",
            "effect.requested",
            request=2,
            intent="book-table-1",
            key="k",
            payload={},
            tool=2,
            latency_us=0,
        ),
        envelope(
            9,
            2_000_000,
            "backend",
            "effect.deduplicated",
            request=2,
            intent="book-table-1",
            key="k",
            receipt="R1",
        ),
        envelope(
            10,
            2_100_000,
            "world",
            "tool.returned",
            tool=2,
            name="look",
            output=None,
            text="",
            expects_reply=False,
        ),
        envelope(
            11, 3_000_000, "world", "tool.called", tool=3, name="book", call_id="c", arguments={}
        ),
        envelope(
            12,
            3_000_000,
            "backend",
            "effect.requested",
            request=3,
            intent="book-table-1",
            key=None,
            payload={},
            tool=3,
            latency_us=0,
        ),
        envelope(
            13, 3_100_000, "backend", "effect.failed", request=3, intent="book-table-1", key=None
        ),
        envelope(14, 3_200_000, "world", "tool.cancelled", tool=3, name="book"),
        envelope(
            15, 4_000_000, "world", "tool.called", tool=4, name="look", call_id="d", arguments={}
        ),
        envelope(
            16,
            4_000_000,
            "backend",
            "effect.requested",
            request=4,
            intent="book-table-1",
            key=None,
            payload={},
            tool=4,
            latency_us=9_000_000,
        ),
        envelope(17, 5_000_000, "world", "odd.kind"),
    ]


def test_tool_and_write_outcomes_are_told_apart():
    events = every_outcome()
    doc = well_formed(report.render(events))
    for said in (
        "tool 1 book({}) [body replaced by one write]",
        "raised TimeoutError: gave up",
        "caller of write 1 timed out after 0.200 s",
        "committed, receipt R1, effect 1 for this intent",
        "tool 2 look({})",
        "returned null (asks for no reply)",
        "deduplicated, the key had committed: receipt R1",
        "failed: the backend rejected it, nothing committed",
        "cancelled before it returned",
        "no outcome in the ledger",
        "other events",
        "odd.kind",
    ):
        assert said in doc.text, said
    assert "tool 2 look({}) [body" not in doc.text
    bars = [
        attrs["class"].split()
        for tag, attrs in doc.tags
        if tag == "rect" and "bar-" in (attrs.get("class") or "") and "x" in attrs
    ]
    bars = [classes for classes in bars if classes[0] in doc.timeline]
    tools = [classes for classes in bars if classes[0] == "bar-tool-open"]
    writes = [classes for classes in bars if classes[0] == "bar-write-open"]
    # A cancelled tool and one with no outcome are drawn open; so is a write with no outcome.
    assert sum("bar-tool" in classes for classes in tools[-4:]) == 2
    assert sum("bar-write" in classes for classes in writes[-4:]) == 3
    for index in range(len(events)):
        assert f"s{index}" in doc.timeline


def test_a_long_run_is_drawn_at_a_smaller_scale():
    header = golden("lk-1.8.12.barge_in_mid_word")[0]["data"]
    events = [
        envelope(0, 0, "harness", "run.start", **header),
        envelope(1, 0, "world", "user.origin", by="stt"),
        envelope(2, 600_000_000, "framework", "fw.close", reason="done"),
    ]
    page = report.render(events)
    doc = well_formed(page)
    width = next(
        float(attrs["width"]) for tag, attrs in doc.tags if tag == "svg" and "role" in attrs
    )
    assert width < report.MAX_WIDTH + 400
    assert "16 px per second" in doc.text
    # A run of twenty seconds keeps the full scale, with a label every second.
    doc = well_formed(report.render(golden("lk-1.5.6.book_then_interrupt")))
    assert "120 px per second" in doc.text
    labels = re.findall(
        r'<text class="t ax"[^>]*>(\d+)</text>',
        report.render(golden("lk-1.5.6.book_then_interrupt")),
    )
    assert {str(second) for second in range(1, 21)} <= set(labels)


def test_the_overall_result_is_never_carried_by_colour_alone():
    for outcome in (PASS, FAIL, UNOBSERVABLE, NOT_EXERCISED, "invalid"):
        badge = report._badge(outcome)
        assert report.OUTCOME_WORDS[outcome] in badge, "the word is in the badge"
    shapes = {report._status_shape(o) for o in (PASS, FAIL, UNOBSERVABLE, NOT_EXERCISED, "invalid")}
    assert len(shapes) == 5, "each outcome has its own shape"
