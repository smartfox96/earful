"""Run a scenario through a Pipecat pipeline and record its ledger."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar

import pipecat
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.workers.runner import WorkerRunner

from ..core import backend as backends
from ..core.backend import BackendMode, Effect, EffectBackend
from ..core.checker import Report, check
from ..core.ledger import Ledger, ledger_hash
from ..core.predicate import Predicate
from ..core.schema import validity
from ..core.timebase import to_seconds, to_us
from ..scripted.case import Delay, Fault, Intent, InvalidRun, check_case, declared_intents
from ..scripted.clock import (
    VirtualTimeLoop,
    left_the_clock,
    now_us,
    patch_report,
    run_real,
    run_virtual,
)
from ..scripted.model import Policy
from ..scripted.world import UserSpeech, WorldOrigin, sleep_until
from . import pipeline as pipelines
from .clock import VirtualClock, wait_for_of_python_3_12
from .pipeline import Ends
from .providers import SAMPLE_RATE, ScriptedLLM, ScriptedSTT, ScriptedUser, StampedTTS, summarize
from .sink import ClockedOutput
from .tools import ToolWitness


class Tap(FrameProcessor):
    """The framework's own account of the turn, at the end of the pipeline. Explanatory:
    recorded as ``fw.*`` events, which decide no verdict."""

    _NAMES: ClassVar[dict[type[Frame], str]] = {
        UserStartedSpeakingFrame: "user_turn_started",
        UserStoppedSpeakingFrame: "user_turn_stopped",
        BotStartedSpeakingFrame: "bot_started_speaking",
        BotStoppedSpeakingFrame: "bot_stopped_speaking",
        InterruptionFrame: "interruption",
    }

    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self._ledger = ledger
        self.user_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        name = self._NAMES.get(type(frame))
        if name is not None and direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
                self.user_speaking = isinstance(frame, UserStartedSpeakingFrame)
            self._ledger.append("framework", f"fw.{name}", created_us=self._ledger.now_us())
        await self.push_frame(frame, direction)


@dataclass
class Case:
    """A scenario for a Pipecat bot. ``bot`` is the application: handed the model
    service, it registers its functions and returns the context the bot starts with.
    ``pipeline`` is the application's pipeline around Earful's ends."""

    name: str
    bot: Callable[[LLMService], LLMContext]
    speeches: list[UserSpeech]
    policy: Policy
    intents: list[Intent] = field(default_factory=list)
    effects: dict[str, Effect] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    delays: list[Delay] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    hangup: float | None = None
    backend_mode: BackendMode = "plain"
    seconds_per_letter: float = 0.06
    tts_ttfb: float = 0.2
    vad_start_secs: float = 0.2  # Pipecat's VADParams.start_secs
    vad_stop_secs: float = 0.2  # Pipecat's VADParams.stop_secs
    tts_word_timestamps: bool = False  # whether the voice says when each word starts
    user_speech_timeout: float = 0.6  # SpeechTimeoutUserTurnStopStrategy
    user_turn_stop_timeout: float = 5.0  # LLMUserAggregatorParams
    drain_delay: float = 5.0
    drain_timeout: float = 30.0
    # The application's processors in order, around Earful's ends. None: the standard seven.
    pipeline: pipelines.Build | None = None


@dataclass
class RunResult:
    case: str
    seed: int
    ledger: Ledger
    hash: str
    invalid: list[str]
    harness_errors: list[str]

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.ledger.events

    @cached_property
    def report(self) -> Report:
        return check(self.ledger.events)

    def assert_contracts(
        self, *, allow: Iterable[Predicate | str] | str = (), require: Iterable[str] | str = ()
    ) -> None:
        """Raise ``ContractFailure``, an ``AssertionError``, if a contract rule broke.
        ``allow`` names rules that may break, as ``CONTRACT/RULE``. ``require`` names
        contracts, such as ``A2``, that must get a pass or a fail."""
        __tracebackhide__ = True
        self.report.assert_contracts(
            allow=allow,
            require=require,
            what=f"{self.case} (seed {self.seed}, pipecat-ai {pipecat.__version__})",
        )


def _header(case: Case, tools: set[str], options: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(case.effects.keys() - tools)
    if unknown:
        raise ValueError(
            f"effects are declared for {unknown}, which are not functions the bot registers; "
            f"it registers {sorted(tools)}"
        )
    intents = declared_intents(case.intents, case.effects, case.speeches)
    return {
        "speeches": [
            {
                "speech": i,
                "start_us": sp.start_us,
                "end_us": sp.end_us,
                "transcript": sp.transcript,
                "stt_delay_us": sp.stt_delay_us,
                "expects_reply": sp.owes_reply,
                "backchannel": sp.backchannel,
                "retires": list(sp.retires),
            }
            for i, sp in enumerate(sorted(case.speeches, key=lambda sp: sp.start))
        ],
        "hangup_us": None if case.hangup is None else to_us(case.hangup),
        "intents": [
            {"intent": it.intent, "tools": list(it.tools), "authorized": it.authorized}
            for it in intents
        ],
        "witnessed_tools": sorted(tools),
        **(
            {
                "effects": [
                    {"tool": name, "intent": effect.intent}
                    for name, effect in sorted(case.effects.items())
                ]
            }
            if case.effects
            else {}
        ),
        "delays": [
            {"of": d.of, "nth": d.nth, "us": to_us(d.seconds)}
            for d in sorted(case.delays, key=lambda d: (d.of, d.nth))
        ],
        "session": {
            # Pipecat interrupts when the turn starts, which the VAD strategy does as
            # soon as the VAD reports speech: after start_secs of it, and no later.
            "min_interruption_duration_us": to_us(case.vad_start_secs),
            "false_interruption_timeout_us": None,  # Pipecat does not resume a reply
            # The turn ends user_speech_timeout after the VAD reports the end of speech.
            "min_endpointing_delay_us": to_us(case.user_speech_timeout),
            "max_endpointing_delay_us": to_us(case.user_turn_stop_timeout),
            "turn_end_delay_us": to_us(case.vad_stop_secs) + to_us(case.user_speech_timeout),
            "turn_detection": None,
            "extra_options": options,
        },
        "faults": [
            {"of": f.of, "nth": f.nth, "us": None if f.seconds is None else to_us(f.seconds)}
            for f in sorted(case.faults, key=lambda f: (f.of, f.nth))
        ],
        "providers": {
            "vad": {
                "min_speech_us": to_us(case.vad_start_secs),
                "min_silence_us": to_us(case.vad_stop_secs),
            },
            "tts": {
                "seconds_per_letter_us": to_us(case.seconds_per_letter),
                "ttfb_us": to_us(case.tts_ttfb),
                "word_timestamps": case.tts_word_timestamps,
            },
            "sink": {"can_pause": False},
            "backend": {"mode": case.backend_mode},
        },
    }


QUIET_FOR_US = 1_000_000
# On the real clock a sleep ends late. An output with this much audio buffered plays on.
REAL_CLOCK_LATE_US = 20_000
# Pipecat names an audio context by a random id and a processor by a count kept per process.
_UNSTABLE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?<=#)\d+")


async def _quiet(busy: Callable[[], bool]) -> None:
    """Wait until nothing has been busy for a second: no tool running, no reply being
    generated or synthesized, nothing playing. Pipecat's EndFrame cancels a function call
    that is still running, so a scenario must not end the pipeline over one."""
    since = now_us()
    while now_us() - since < QUIET_FOR_US:
        await asyncio.sleep(0.1)
        if busy():
            since = now_us()


async def _run(case: Case, seed: int) -> RunResult:
    if not callable(case.bot):
        raise ValueError(  # noqa: TRY004 - a case that is refused, as check_case refuses one
            "bot is a function that is handed the model service and returns the context; "
            f"got an object of {type(case.bot).__name__}"
        )
    check_case(case)
    random.seed(seed)
    loop = asyncio.get_running_loop()
    virtual = isinstance(loop, VirtualTimeLoop)
    if virtual:
        ledger = Ledger(now_us)
    else:  # a real-clock control run: times are from the start of the run
        began_us = now_us()
        ledger = Ledger(lambda: now_us() - began_us)
    errors = [
        f"{name} reads the wall clock and could not be given the virtual one"
        for name in (patch_report().unpatched if virtual else ())
    ]

    def declared_us(of: str) -> dict[int, int]:
        return {d.nth: to_us(d.seconds) for d in case.delays if d.of == of}

    backend = EffectBackend(
        ledger,
        mode=case.backend_mode,
        latencies_us=declared_us("backend.latency"),
        faults={
            f.nth: (f.of, None if f.seconds is None else to_us(f.seconds)) for f in case.faults
        },
    )
    origin = WorldOrigin(ledger, errors, tolerance_us=0 if virtual else 5_000)
    witness = ToolWitness(ledger, case.effects)
    left = asyncio.Event()  # set when the user hangs up
    user = ScriptedUser(
        ledger,
        case.speeches,
        origin,
        left,
        start_secs=case.vad_start_secs,
        stop_secs=case.vad_stop_secs,
    )
    stt = ScriptedSTT(ledger, case.speeches, origin, left)
    model = ScriptedLLM(
        ledger,
        case.policy,
        errors,
        witness,
        ttft_us=declared_us("llm.ttft"),
        duration_us=declared_us("llm.duration"),
    )
    voice = StampedTTS(
        ledger,
        model.register,
        seconds_per_letter=case.seconds_per_letter,
        ttfb=case.tts_ttfb,
        ttfb_us=declared_us("tts.ttfb"),
        word_timestamps=case.tts_word_timestamps,
    )
    output = ClockedOutput(ledger, voice.table, late_us=0 if virtual else REAL_CLOCK_LATE_US)

    token = backends.install(backend)
    reported = 0  # errors the pipeline reported about itself
    try:
        context = case.bot(model)
        ends = Ends(user, stt, model, voice, output, context)
        timeouts = (case.user_speech_timeout, case.user_turn_stop_timeout)
        build = case.pipeline or pipelines.standard(*timeouts)
        plan = pipelines.inspect(build(ends), ends, *timeouts, own=case.pipeline is not None)
        model.witness_tools(context.tools)
        header = _header(case, witness.names, plan.options)
        assistant = plan.assistant

        def committed(_aggregator: Any, message: Any) -> None:
            if message.interrupted and message.content:
                for item in reversed(context.get_messages()):
                    if (
                        isinstance(item, dict)
                        and item.get("role") == "assistant"
                        and item.get("content") == message.content
                    ):
                        model.interrupted[id(item)] = item
                        break

        assistant.add_event_handler("on_assistant_turn_stopped", committed)
        tap = Tap(ledger)
        pipeline = Pipeline([*plan.processors, tap])
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_out_sample_rate=SAMPLE_RATE),
            clock=VirtualClock() if virtual else None,
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
        )

        def pipeline_error(_worker: PipelineWorker, frame: ErrorFrame) -> None:
            nonlocal reported
            if any(exc is frame.exception for exc in witness.exceptions):
                return  # a tool raised: the witness has recorded it
            if frame.exception is not None or frame.fatal:
                errors.append(_UNSTABLE.sub("#", frame.error))  # a processor broke
                return
            # Something Pipecat says of itself, such as a voice that sent no audio in time.
            reported += 1
            ledger.append(
                "framework",
                "fw.error",
                created_us=ledger.now_us(),
                error=_UNSTABLE.sub("#", frame.error),
            )

        worker.add_event_handler("on_pipeline_error", pipeline_error)
        ledger.append(
            "harness",
            "run.start",
            case=case.name,
            seed=seed,
            framework={"name": "pipecat-ai", "version": pipecat.__version__},
            clock="virtual" if virtual else "real",
            scenario=header,
            contracts=case.contracts,
        )
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        running = asyncio.ensure_future(runner.run())
        ended = asyncio.ensure_future(stt.done if case.hangup is None else origin.started.wait())
        # A provider can fail before signaling completion. Allow the entire script to
        # play, plus the same startup/scheduling allowance as for pipeline shutdown.
        timeout = case.drain_timeout
        if case.hangup is None:
            last_transcript_us = max((s.end_us + s.stt_delay_us for s in case.speeches), default=0)
            timeout += to_seconds(last_transcript_us)
        await asyncio.wait({running, ended}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if running.done():
            errors.append("the pipeline ended before the scenario did")
            ended.cancel()
            running.result()
        elif not ended.done():
            errors.append(f"the scripted input did not complete within {timeout:g} s")
            ended.cancel()
            left.set()
            await worker.cancel()
        elif case.hangup is None:
            await asyncio.sleep(case.drain_delay)

            def busy() -> bool:
                return bool(
                    not user.done
                    or tap.user_speaking
                    or witness.in_flight
                    or model.generating
                    or voice.synthesizing
                    or output.playing
                )

            try:
                await asyncio.wait_for(_quiet(busy), timeout=case.drain_timeout)
            except asyncio.TimeoutError:
                errors.append(f"the bot was still busy {case.drain_timeout} s after the scenario")
            await worker.queue_frame(EndFrame())
        else:
            # The user leaves. A Pipecat application cancels its pipeline when the client
            # disconnects, which is what the transports' examples do.
            assert origin.t0_us is not None
            await sleep_until(origin.t0_us, to_us(case.hangup))
            left.set()
            ledger.append("world", "user.hangup")
            await worker.cancel()
        try:
            await asyncio.wait_for(running, timeout=case.drain_timeout)
        except asyncio.TimeoutError:
            errors.append(f"the pipeline did not end within {case.drain_timeout} s")
        try:
            await asyncio.wait_for(backend.settle_in_flight(), timeout=case.drain_timeout)
        except asyncio.TimeoutError:
            errors.append("backend writes did not settle before the drain timeout")
        if case.hangup is not None:
            await asyncio.sleep(case.drain_delay)
        voice.close_all()
        # The header states the turn strategies the run began with.
        try:
            now = pipelines.turns(plan.user, *timeouts)
            changed = ", ".join(f"{k} is {v}" for k, v in now.items() if plan.options[k] != v)
        except ValueError as exc:
            changed = str(exc)
        if changed:
            errors.append(f"the pipeline changed its turn strategies during the run: {changed}")
        ledger.append(
            "harness",
            "history.final",
            read_from="LLMContext.get_messages",
            items=summarize(context.get_messages(), model.error_results, model.interrupted),
        )
    finally:
        await backend.cancel_in_flight()
        backends.uninstall(token)
    errors += left_the_clock(loop)
    ledger.append(
        "harness",
        "run.end",
        stamp_errors=output.stamp_errors,
        executor_calls=loop.executor_calls if isinstance(loop, VirtualTimeLoop) else None,
        underruns=0,
        harness_errors=errors,
        framework_warnings=reported,
    )
    return RunResult(
        case.name, seed, ledger, ledger_hash(ledger.events), validity(ledger.events), errors
    )


def run_case(
    case: Case, seed: int = 0, *, virtual: bool = True, require_valid: bool = True
) -> RunResult:
    """Run a fresh pipeline and raise InvalidRun if its ledger cannot be judged."""
    with wait_for_of_python_3_12():
        if virtual:
            result = run_virtual(lambda: _run(case, seed))
        else:
            result = run_real(lambda: _run(case, seed), timeout=120)
    if result.invalid and require_valid:
        raise InvalidRun(result.invalid, result)
    return result
