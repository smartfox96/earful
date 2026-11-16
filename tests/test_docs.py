"""docs/ledger.md and the JSON Schema describe the same ledger."""
import re
from pathlib import Path

from earful.core.schema import kinds, load_schema

DOC = (Path(__file__).resolve().parents[1] / "docs" / "ledger.md").read_text()
MENTIONED = set(re.findall(r"`([^`\s]+)`", DOC))
_KIND = re.compile(r"^(run|history|user|tts|tool|llm|sink|playout|effect)\.[a-z_]+$")


def _field_names(schema, defs):
    """Every property name in ``schema``, following references."""
    names = set()
    if "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[1]]
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key, []):
            names |= _field_names(option, defs)
    for key in ("items", "then", "else", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            names |= _field_names(schema[key], defs)
    for name, sub in schema.get("properties", {}).items():
        names.add(name)
        if isinstance(sub, dict):
            names |= _field_names(sub, defs)
    return names


def test_every_kind_and_field_in_the_schema_is_documented():
    defs = load_schema()["$defs"]
    missing = [kind for kind in kinds() if kind not in MENTIONED]
    for kind in kinds():
        missing += [f"{kind}: {name}" for name in sorted(_field_names(defs[kind], defs) - MENTIONED)]
    assert not missing, f"docs/ledger.md does not mention {missing}"


def test_every_kind_the_document_names_is_in_the_schema():
    documented = {token for token in MENTIONED if _KIND.match(token)}
    assert documented and documented <= set(kinds()), documented - set(kinds())


def test_the_envelope_is_documented():
    assert set(load_schema()["required"]) <= MENTIONED
