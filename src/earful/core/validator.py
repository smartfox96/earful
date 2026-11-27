"""The JSON Schema validator of Earful's formats."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from jsonschema import Draft202012Validator, validators

# Objects found valid against a reference while checked_once is open.
_VALID: ContextVar[set[tuple[int, str]] | None] = ContextVar("earful_valid", default=None)
_REF = Draft202012Validator.VALIDATORS["$ref"]


def _ref_once(validator: Any, ref: str, instance: Any, schema: dict[str, Any]) -> Iterator[Any]:
    """``$ref``, checking an object only once against the same reference."""
    seen = _VALID.get()
    if seen is None or not isinstance(instance, dict):
        yield from _REF(validator, ref, instance, schema)
        return
    key = (id(schema), repr(instance))  # repr keeps 1, 1.0 and True apart, as the schema does
    if key in seen:
        return
    errors = list(_REF(validator, ref, instance, schema))
    if not errors:
        seen.add(key)
    yield from errors


IntegerValidator = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator,
    validators={"$ref": _ref_once},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, n: type(n) is int),
)


@contextmanager
def checked_once() -> Iterator[None]:
    """Within the block an object found valid against a reference is not checked again."""
    # A ledger's model requests each repeat the context of the one before.
    token = _VALID.set(set())
    try:
        yield
    finally:
        _VALID.reset(token)
