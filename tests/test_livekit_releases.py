"""The livekit-agents release the adapter accepts, warns about or refuses at import."""

import pytest

pytest.importorskip("livekit.agents")

from earful.scripted.releases import SUPPORT


def test_the_livekit_adapter_warns_once_when_it_is_imported_on_an_untested_release(imported_on):
    imported_on("livekit-agents", "1.8.999").warns_once()


def test_the_command_refuses_livekit_agents_outside_the_range_before_the_adapter_loads(imported_on):
    imported_on("livekit-agents", "1.9.0").is_refused()


def test_a_name_an_untested_livekit_release_lacks_is_reported_with_the_tested_releases(imported_on):
    module = "livekit.agents.voice.transcription.synchronizer"
    said = imported_on("livekit-agents", "1.8.999").lacks(module)
    assert said.startswith(
        "ImportError: earful.livekit cannot be imported on livekit-agents 1.8.999, which is "
        f"not a tested release (import of {module} halted"
    )
    assert said.endswith(f"The tested releases are: {', '.join(SUPPORT['livekit-agents'].tested)}")
    tested = SUPPORT["livekit-agents"].tested[-1]
    assert "tested release" not in imported_on("livekit-agents", tested).lacks(module)
