"""The committed reference ledgers: this machine reproduces them, and they validate."""

from pathlib import Path

import livekit.agents as lk
import pytest

from earful.core.ledger import first_difference, ledger_hash, normalize, read_jsonl
from earful.core.schema import validity
from earful.livekit.cases import CASES
from earful.livekit.harness import run_case

# Change these only with scripts/check_all.sh --update, and review the diff.
GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize("name", sorted(CASES))
def test_run_reproduces_the_committed_ledger(name, reference_ledger):
    path = reference_ledger(name)
    lines = normalize(run_case(CASES[name]()).ledger.events)
    assert first_difference(path.read_text("ascii").splitlines(), lines) is None


def test_only_a_release_with_no_reference_ledgers_at_all_skips_the_comparison(
    reference_ledger, monkeypatch
):
    monkeypatch.setattr(lk, "__version__", "1.8.999")
    with pytest.raises(pytest.skip.Exception, match=r"livekit-agents 1\.8\.999 has no reference"):
        reference_ledger("barge_in_mid_word")
    monkeypatch.setattr(lk, "__version__", "1.8.12")
    assert reference_ledger("barge_in_mid_word").name.startswith("lk-1.8.12.barge_in_mid_word.")
    with pytest.raises(BaseException) as missing:  # a skip here would pass unnoticed
        reference_ledger("no_such_case")
    assert isinstance(missing.value, AssertionError)
    assert "no reference ledger of no_such_case" in str(missing.value)


def _with_ids(order, first="item_aaaaaaaaaaaa", second="item_bbbbbbbbbbbb"):
    """Events in which the framework's ids and an application's id look alike."""
    item = {"type": "function_call_output", "id": second, "output": order, "call_id": order}
    third = first[:-1] + "9"
    return [
        {"source": "llm", "kind": "llm.request", "data": {"call": 1, "items": [{"id": first}]}},
        {"source": "world", "kind": "tool.returned", "data": {"output": {"id": order}}},
        {"source": "backend", "kind": "effect.requested", "data": {"payload": {"order": order}}},
        {"source": "framework", "kind": "fw.conversation_item_added", "data": {"item": item}},
        {"source": "framework", "kind": "fw.speech_created", "data": {"speech_id": third}},
        {"source": "harness", "kind": "history.final", "data": {"items": [item, {"id": first}]}},
    ]


def test_only_the_frameworks_own_identifiers_are_replaced_by_ordinals():
    order = "order_0123456789ab"
    lines = normalize(_with_ids(order))
    assert '"items":[{"id":"item_#1"}]' in lines[0]
    assert order in lines[1] and order in lines[2]  # what the application wrote stays
    assert f'"call_id":"{order}","id":"item_#2","output":"{order}"' in lines[3]
    assert '"speech_id":"item_#3"' in lines[4]
    assert lines[5].count("item_#2") == lines[5].count("item_#1") == 1
    assert "aaaa" not in "".join(lines) and "bbbb" not in "".join(lines)
    # The ordinals are the same whatever the framework drew; an application's id is data.
    assert ledger_hash(_with_ids(order, "item_cccccccccccc", "item_dddddddddddd")) == ledger_hash(
        _with_ids(order)
    )
    assert ledger_hash(_with_ids("order_ba9876543210")) != ledger_hash(_with_ids(order))


def test_every_committed_ledger_is_a_valid_run():
    paths = sorted(GOLDEN.glob("*.ledger.normalized.jsonl"))
    assert paths
    for path in paths:
        assert validity(read_jsonl(path)) == [], path.name


def test_a_normalized_ledger_file_is_its_own_normal_form():
    for path in sorted(GOLDEN.glob("*.ledger.normalized.jsonl")):
        assert normalize(read_jsonl(path)) == path.read_text("ascii").splitlines(), path.name
