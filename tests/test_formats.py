"""Version 1 readers tolerate additive metadata and still reject unsafe inputs."""

import copy
import json
from pathlib import Path

import pytest

from earful.core import bundle
from earful.core import case as cases
from earful.core.ledger import run_id
from earful.core.schema import validate, validity
from earful.livekit import scenario
from earful.livekit.cases import barge_in_mid_word
from earful.livekit.harness import run_case


@pytest.fixture(scope="module")
def ledger():
    return run_case(barge_in_mid_word()).ledger.events


def test_future_minor_ledger_metadata_is_ignored_without_changing_validity(ledger):
    events = copy.deepcopy(ledger)
    events[0]["data"]["scenario"]["speeches"][0]["diagnostic"] = {"future": True}
    events[0]["data"]["future"] = "metadata"
    run = run_id(events[0]["data"])
    for event in events:
        event.update(schema_minor=17, future="metadata", run=run)
        event["data"]["future_detail"] = [1, 2, 3]
    # All header changes participate in run identity, including unknown metadata.
    run = run_id(events[0]["data"])
    for event in events:
        event["run"] = run
    assert validity(events) == []


def test_future_minor_case_metadata_is_ignored_but_unknown_settings_are_refused():
    data = scenario.default("earful.livekit.cases:barge_in_mid_word")
    data.update(schema_minor=17, future="metadata")
    data["speeches"][0]["diagnostic"] = {"future": True}
    assert cases.validate(data) == []
    assert scenario.build(data).speeches[0].transcript == data["speeches"][0]["transcript"]
    data["settings"]["future_execution_setting"] = True
    with pytest.raises(ValueError, match="unknown setting"):
        scenario.build(data)


def test_minor_zero_may_be_omitted(ledger):
    events = copy.deepcopy(ledger)
    for event in events:
        event.pop("schema_minor", None)
    assert validity(events) == []
    data = scenario.default("earful.livekit.cases:barge_in_mid_word")
    data.pop("schema_minor")
    assert cases.validate(data) == []


def test_future_minor_bundle_metadata_is_ignored():
    path = next((Path(__file__).parent / "bundles").glob("*/manifest.json"))
    manifest = json.loads(path.read_text())
    manifest.update(schema_minor=17, future="metadata")
    manifest["minimal"]["diagnostic"] = {"future": True}
    assert bundle.validate(manifest) == []
    manifest["minimal"]["hash"] = 12
    assert bundle.validate(manifest)


@pytest.mark.parametrize("minor", [-1, True, "1"])
def test_minor_version_must_be_a_nonnegative_integer(ledger, minor):
    events = copy.deepcopy(ledger)
    events[0]["schema_minor"] = minor
    assert validate(events)
    data = scenario.default("earful.livekit.cases:barge_in_mid_word")
    data["schema_minor"] = minor
    assert cases.validate(data)


def test_a_ledger_cannot_mix_minor_versions(ledger):
    events = copy.deepcopy(ledger)
    events[1]["schema_minor"] = 1
    assert any("schema_minor differs" in error for error in validate(events))


def test_unknown_major_versions_and_missing_required_fields_are_rejected(ledger):
    events = copy.deepcopy(ledger)
    events[0]["schema"] = "earful.ledger/2"
    assert validate(events)
    data = scenario.default("earful.livekit.cases:barge_in_mid_word")
    data["schema"] = "earful.case/2"
    assert cases.validate(data)
    data["schema"] = "earful.case/1"
    del data["speeches"]
    assert cases.validate(data)


def test_unknown_event_kinds_are_rejected_even_in_a_future_minor(ledger):
    for kind in ("future.behaviour", 7, None):
        events = copy.deepcopy(ledger)
        for event in events:
            event["schema_minor"] = 17
        events[1].update(kind=kind, source="framework")
        assert validate(events)
