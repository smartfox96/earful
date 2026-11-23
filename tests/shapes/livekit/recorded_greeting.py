"""An agent that greets the caller with recorded audio: session.say with the text and its
audio frames."""

from __future__ import annotations

from collections.abc import AsyncIterator

import livekit.agents as lk
from livekit import rtc

from earful.livekit import Case, ScriptedModel, UserSpeech
from shapes.corpus import invalid, judged


class Greeter(lk.Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You run a shop.")

    async def on_enter(self) -> None:
        async def recorded() -> AsyncIterator[rtc.AudioFrame]:
            for _ in range(50):  # half a second
                pcm = b"\x10\x00" * 240
                yield rtc.AudioFrame(
                    pcm, sample_rate=24000, num_channels=1, samples_per_channel=240
                )

        self.session.say("Welcome to the shop.", audio=recorded())


def case() -> Case:
    return Case(
        name="recorded_greeting",
        agent=Greeter,
        speeches=[UserSpeech(start=2.0, end=3.0, transcript="Hello there.")],
        policy=ScriptedModel().otherwise(say="How can I help?"),
    )


SHOULD = judged()
# Recorded audio carries no identity, and is taken for altered audio.
DEFECT = invalid("reached the sink altered")
