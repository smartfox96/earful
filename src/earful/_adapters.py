"""Select an installed adapter from a scenario or a saved bundle."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

from .scripted.scenario import reference_of, resolve

_ADAPTERS = {"livekit-agents": "earful.livekit", "pipecat-ai": "earful.pipecat"}


def by_framework(name: str) -> ModuleType:
    if name not in _ADAPTERS:
        raise ValueError(f"no Earful adapter for framework {name!r}")
    return importlib.import_module(_ADAPTERS[name])


def select(source: Any) -> tuple[ModuleType, Any]:
    """Build the first case once and preserve its factory for searches and workers."""
    make = resolve(source) if isinstance(source, str) else source if callable(source) else None
    case = make() if make is not None else source
    for module_name in _ADAPTERS.values():
        adapter = sys.modules.get(module_name)
        if adapter is None:
            continue
        if isinstance(source, adapter.Scenario):
            return adapter, source
        if isinstance(case, adapter.Case):
            break
    else:
        raise ValueError(f"a scenario must provide an Earful Case; got {type(case).__name__}")

    if make is None:
        return adapter, adapter.Scenario.of(case)
    reference = source if isinstance(source, str) else reference_of(make)
    first = [case]

    def build() -> Any:
        return first.pop() if first else make()

    return adapter, adapter.Scenario(build, reference)
