"""Run one scripted case against a real AgentSession and return its ledger."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

import livekit.agents as lk
from livekit import rtc
from livekit.agents import utils

from ..core import backend as backends
from ..core.backend import BackendMode, Effect, EffectBackend
from ..core.checker import Report, check
from ..core.ledger import Ledger, ledger_hash
from ..core.predicate import Predicate
from ..core.schema import validity
from ..core.timebase import to_seconds, to_us
from ..scripted.case import DELAYS, Delay, Fault, Intent, InvalidRun, check_case, declared_intents
from ..scripted.clock import (
    WALL_CLOCK_EPOCH,
    ClockPatchReport,
    VirtualTimeLoop,
    left_the_clock,
    now_us,
    patch_report,
    run_virtual,
)
from . import tools
from ._compat import TranscriptSynchronizer, io
from .providers import (
    Policy,
    ScriptedLLM,
    ScriptedSTT,
    ScriptedVAD,
    StampedTTS,
    UserSpeech,
    WorldOrigin,
    summarize_item,
)
from .sink import ClockedSink

if TYPE_CHECKING:
    from livekit.agents.voice.events import EventTypes

# A real-clock control run that has not finished after this many seconds has hung.
REAL_CLOCK_TIMEOUT = 120.0
# On the real clock, VAD and STT see the first frame microseconds apart.
REAL_CLOCK_ORIGIN_TOLERANCE_US = 5_000

# Session options a Case declares through its own fields: nothing else may set them.
_DECLARED_OPTIONS = frozenset(
    {
        "vad",
        "stt",
        "llm",
        "tts",
        "turn_handling",
        "turn_detection",
        "min_endpointing_delay",
        "max_endpointing_delay",
        "min_interruption_duration",
        "false_interruption_timeout",
    }
)


@dataclass
class Case:
    """Everything that defines a run: the same case and seed give the same ledger.
    ``agent`` takes no arguments and is called once per run. Times are in seconds."""

    name: str
    agent: Callable[[], lk.Agent]
    speeches: list[UserSpeech]
    policy: Policy
    intents: list[Intent] = field(default_factory=list)
    # Tool name -> the durable write a call of that tool makes, in place of its body.
    effects: dict[str, Effect] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> policy values
    delays: list[Delay] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    # When the user hangs up, in seconds from the origin. No speech may end after it.
    hangup: float | None = None
    backend_mode: BackendMode = "plain"
    seconds_per_letter: float = 0.06  # the scripted voice: a five-letter word takes 0.3 s
    tts_ttfb: float = 0.2
    vad_min_speech: float = 0.05
    vad_min_silence: float = 0.5
    sink_can_pause: bool = True
    min_interruption_duration: float = 0.5
    false_interruption_timeout: float = 2.0
    min_endpointing_delay: float = 0.5
    max_endpointing_delay: float = 6.0
    drain_delay: float = 5.0
    # Seconds the session has to finish once the scenario is over, before it is cut off.
    drain_timeout: float = 30.0
    sink: type[ClockedSink] = ClockedSink  # a faulty output is a subclass
    # Further AgentSession options, other than those a Case declares through its own fields.
    session_kwargs: dict[str, Any] = field(default_factory=dict)

    def check(self) -> None:
        """Raise ``ValueError`` for a scenario that contradicts itself."""
        clash = sorted(_DECLARED_OPTIONS & self.session_kwargs.keys())
        if clash:
            raise ValueError(f"session_kwargs may not set {clash}: use the Case fields")
        check_case(self)


@dataclass
class RunResult:
    case: str
    seed: int
    ledger: Ledger
    hash: str
    versions: dict[str, str]
    clock: ClockPatchReport
    stamp_errors: int
    executor_calls: int | None
    underruns: int
    harness_errors: list[str]
    framework_warnings: list[str]
    invalid: list[str]  # reasons the run may not be used as evidence; empty when valid

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.ledger.events

    @cached_property
    def report(self) -> Report:
        """The verdict of every contract for this run."""
        return check(self.ledger.events)

    def assert_contracts(
        self, *, allow: Iterable[Predicate | str] | str = (), require: Iterable[str] | str = ()
    ) -> None:
        """Raise ``ContractFailure``, an ``AssertionError``, if a contract rule broke.
        ``allow`` names rules that may break, as ``CONTRACT/RULE``. ``require`` names
        contracts, such as ``A2``, that must get a pass or a fail."""
        __tracebackhide__ = True  # pytest then shows the test's own line, not this one
        framework = self.versions["livekit-agents"]
        self.report.assert_contracts(
            allow=allow,
            require=require,
            what=f"{self.case} (seed {self.seed}, livekit-agents {framework})",
        )


class _ScriptedAudioInput(io.AudioInput):
    def __init__(self) -> None:
        super().__init__(label="EarfulScriptedInput")
        self._ch = utils.aio.Chan[rtc.AudioFrame]()

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._ch.__anext__()

    def push_silence(self, seconds: float, sample_rate: int = 16000) -> None:
        n = round(sample_rate * seconds)
        self._ch.send_nowait(
            rtc.AudioFrame(
                data=b"\x00\x00" * n, sample_rate=sample_rate, num_channels=1, samples_per_channel=n
            )
        )


class _NullTextOutput(io.TextOutput):
    def __init__(self) -> None:
        super().__init__(label="EarfulNullText", next_in_chain=None)

    async def capture_text(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass


def _session_kwargs(case: Case) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if hasattr(lk, "TurnHandlingOptions"):
        kwargs["turn_handling"] = {
            # The default turn detector is an acoustic model: synthetic audio cannot fire it.
            "turn_detection": None,
            "endpointing": lk.EndpointingOptions(
                min_delay=case.min_endpointing_delay, max_delay=case.max_endpointing_delay
            ),
            "interruption": lk.InterruptionOptions(
                # The other mode is a model reached over the network.
                mode="vad",
                min_duration=case.min_interruption_duration,
                false_interruption_timeout=case.false_interruption_timeout,
            ),
        }
        kwargs["aec_warmup_duration"] = None
    else:  # pre-TurnHandlingOptions releases
        kwargs.update(
            min_endpointing_delay=case.min_endpointing_delay,
            max_endpointing_delay=case.max_endpointing_delay,
            min_interruption_duration=case.min_interruption_duration,
            false_interruption_timeout=case.false_interruption_timeout,
        )
    kwargs.update(case.session_kwargs)
    return kwargs


def _declared_session(case: Case) -> dict[str, int | None]:
    return {
        "min_interruption_duration_us": to_us(case.min_interruption_duration),
        "false_interruption_timeout_us": to_us(case.false_interruption_timeout),
        "min_endpointing_delay_us": to_us(case.min_endpointing_delay),
        "max_endpointing_delay_us": to_us(case.max_endpointing_delay),
    }


def _effective_session(session: lk.AgentSession[Any]) -> dict[str, int | None]:
    interruption, endpointing = session.options.interruption, session.options.endpointing

    def us(value: float | None) -> int | None:
        return None if value is None else to_us(value)

    return {
        "min_interruption_duration_us": us(interruption["min_duration"]),
        "false_interruption_timeout_us": us(interruption["false_interruption_timeout"]),
        "min_endpointing_delay_us": us(endpointing["min_delay"]),
        "max_endpointing_delay_us": us(endpointing["max_delay"]),
    }


def _effects(case: Case, tools_found: dict[str, Effect | None]) -> dict[str, Effect]:
    # A misspelt tool name would leave the tool's own body to run.
    unknown = sorted(set(case.effects) - set(tools_found))
    if unknown:
        raise ValueError(
            f"effects are declared for {unknown}, which are not function tools of the agent "
            f"the run starts with; it has {sorted(tools_found)}"
        )
    at_the_tool = {name: found for name, found in tools_found.items() if found is not None}
    return {**at_the_tool, **case.effects}


def _header(case: Case, tools_found: dict[str, Effect | None]) -> dict[str, Any]:
    effects = _effects(case, tools_found)
    intents = declared_intents(case.intents, effects, case.speeches)
    header = _scenario(case, sorted(tools_found), intents)
    if effects:
        # Stated only where there are any: a tool listed here did not run its own body.
        header["effects"] = [
            {"tool": tool, "intent": declared.intent} for tool, declared in sorted(effects.items())
        ]
    return header


def _scenario(case: Case, witnessed_tools: list[str], intents: list[Intent]) -> dict[str, Any]:
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
        "witnessed_tools": witnessed_tools,
        "delays": [
            {"of": delay.of, "nth": delay.nth, "us": to_us(delay.seconds)}
            for delay in sorted(case.delays, key=lambda d: (d.of, d.nth))
        ],
        "session": {
            **_declared_session(case),
            "turn_detection": None,
            "extra_options": {
                name: tools.json_value(case.session_kwargs[name])
                for name in sorted(case.session_kwargs)
            },
        },
        "faults": [
            {
                "of": fault.of,
                "nth": fault.nth,
                "us": None if fault.seconds is None else to_us(fault.seconds),
            }
            for fault in sorted(case.faults, key=lambda f: (f.of, f.nth))
        ],
        "providers": {
            "vad": {
                "min_speech_us": to_us(case.vad_min_speech),
                "min_silence_us": to_us(case.vad_min_silence),
            },
            "tts": {
                "seconds_per_letter_us": to_us(case.seconds_per_letter),
                "ttfb_us": to_us(case.tts_ttfb),
            },
            "sink": {"can_pause": case.sink_can_pause},
            "backend": {"mode": case.backend_mode},
        },
    }


def _created_us(ev: Any) -> int | None:
    created = getattr(ev, "created_at", None)
    return round((created - WALL_CLOCK_EPOCH) * 1_000_000) if created is not None else None


def _tap(session: lk.AgentSession[Any], ledger: Ledger, errors: list[str]) -> None:
    # A handler must never raise into the framework: failures are collected and fail the run.
    def on(name: EventTypes, fields: Callable[[Any], dict[str, Any]]) -> None:
        def handler(ev: Any) -> None:
            try:
                ledger.append("framework", f"fw.{name}", created_us=_created_us(ev), **fields(ev))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"tap {name}: {type(exc).__name__}: {exc}")

        session.on(name, handler)

    on("agent_state_changed", lambda e: {"old": e.old_state, "new": e.new_state})
    on("user_state_changed", lambda e: {"old": e.old_state, "new": e.new_state})
    on("user_input_transcribed", lambda e: {"text": e.transcript, "final": e.is_final})
    on("conversation_item_added", lambda e: {"item": summarize_item(e.item)})
    on(
        "function_tools_executed",
        lambda e: {
            "calls": [summarize_item(c) for c in e.function_calls],
            "outputs": [summarize_item(o) for o in e.function_call_outputs if o is not None],
        },
    )
    on(
        "speech_created",
        lambda e: {
            "speech_id": e.speech_handle.id,
            "source": e.source,
            "user_initiated": e.user_initiated,
        },
    )
    on("agent_false_interruption", lambda e: {})
    on("close", lambda e: {"reason": str(getattr(e.reason, "value", e.reason))})
    on("error", lambda e: {"error": type(e.error).__name__})


async def _hang_up(
    case: Case,
    session: lk.AgentSession[Any],
    origin: WorldOrigin,
    ledger: Ledger,
    witness: tools.ToolWitness,
    backend: EffectBackend,
    errors: list[str],
) -> None:
    assert case.hangup is not None
    await origin.started.wait()
    assert origin.t0_us is not None
    wait_us = origin.t0_us + to_us(case.hangup) - now_us()
    if wait_us > 0:
        await asyncio.sleep(to_seconds(wait_us))
    ledger.append("world", "user.hangup")
    closing = asyncio.ensure_future(session.aclose())
    done, _ = await asyncio.wait({closing}, timeout=case.drain_timeout)
    if not done:
        # Some releases cannot close a session while one of its tools is stuck.
        await witness.cancel_running()
        done, _ = await asyncio.wait({closing}, timeout=case.drain_timeout)
    if not done:
        closing.cancel()
        errors.append(f"the session did not close within {case.drain_timeout} s of the hangup")
    else:
        closing.result()  # a close that raises is the run's error, as at the end of a scenario
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(backend.settle_in_flight(), timeout=case.drain_timeout)
    # An output that has not stopped is given the time a scenario gives one at its end.
    await asyncio.sleep(case.drain_delay)


class _WarningCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.name}: {record.getMessage()}")


async def _run(case: Case, seed: int) -> RunResult:
    case.check()
    random.seed(seed)
    loop = asyncio.get_running_loop()
    virtual = isinstance(loop, VirtualTimeLoop)
    if virtual:
        ledger = Ledger(now_us)
    else:  # real-clock control run: times are relative to the start of the run
        started_us = now_us()
        ledger = Ledger(lambda: now_us() - started_us)

    # Errors in Earful's own simulation or recording. Any of them makes the run invalid.
    harness_errors: list[str] = []
    if virtual:
        harness_errors += [
            f"{name} reads the wall clock and could not be given the virtual one"
            for name in patch_report().unpatched
        ]

    def declared_us(of: str) -> dict[int, int]:
        return {delay.nth: to_us(delay.seconds) for delay in case.delays if delay.of == of}

    backend = EffectBackend(
        ledger,
        mode=case.backend_mode,
        latencies_us=declared_us("backend.latency"),
        faults={
            f.nth: (f.of, None if f.seconds is None else to_us(f.seconds)) for f in case.faults
        },
    )
    witness = tools.ToolWitness(ledger, case.effects)
    warnings = _WarningCollector()
    lk_logger = logging.getLogger("livekit.agents")
    lk_logger.addHandler(warnings)
    witness_token = tools.install(witness)
    backend_token = backends.install(backend)
    try:
        sink = await _play(
            case, seed, ledger, harness_errors, declared_us, witness, backend, virtual=virtual
        )
    finally:
        lk_logger.removeHandler(warnings)
        backends.uninstall(backend_token)
        tools.uninstall(witness_token)

    executor_calls = loop.executor_calls if isinstance(loop, VirtualTimeLoop) else None
    harness_errors += left_the_clock(loop)
    ledger.append(
        "harness",
        "run.end",
        stamp_errors=sink.stamp_errors,
        executor_calls=executor_calls,
        underruns=sink.underruns,
        harness_errors=harness_errors,
        framework_warnings=len(warnings.messages),
    )
    return RunResult(
        case=case.name,
        seed=seed,
        ledger=ledger,
        hash=ledger_hash(ledger.events),
        versions=versions(),
        clock=patch_report(),
        stamp_errors=sink.stamp_errors,
        executor_calls=executor_calls,
        underruns=sink.underruns,
        harness_errors=list(harness_errors),
        framework_warnings=warnings.messages,
        invalid=validity(ledger.events),
    )


async def _play(
    case: Case,
    seed: int,
    ledger: Ledger,
    harness_errors: list[str],
    declared_us: Callable[[str], dict[int, int]],
    witness: tools.ToolWitness,
    backend: EffectBackend,
    *,
    virtual: bool,
) -> ClockedSink:
    world_origin = WorldOrigin(
        ledger, harness_errors, tolerance_us=0 if virtual else REAL_CLOCK_ORIGIN_TOLERANCE_US
    )
    stt = ScriptedSTT(ledger, case.speeches, world_origin)
    model = ScriptedLLM(
        ledger,
        case.policy,
        harness_errors,
        ttft_us=declared_us("llm.ttft"),
        duration_us=declared_us("llm.duration"),
    )
    tts = StampedTTS(
        ledger,
        model.register,
        seconds_per_letter=case.seconds_per_letter,
        ttfb=case.tts_ttfb,
        ttfb_us=declared_us("tts.ttfb"),
    )
    session = lk.AgentSession(
        vad=ScriptedVAD(
            ledger,
            case.speeches,
            world_origin,
            min_speech_duration=case.vad_min_speech,
            min_silence_duration=case.vad_min_silence,
        ),
        stt=stt,
        llm=model,
        tts=tts,
        **_session_kwargs(case),
    )

    audio_in = _ScriptedAudioInput()
    sink = case.sink(ledger, tts.table, can_pause=case.sink_can_pause)
    sync = TranscriptSynchronizer(next_in_chain_audio=sink, next_in_chain_text=_NullTextOutput())
    session.input.audio = audio_in
    session.output.audio = sync.audio_output
    session.output.transcription = sync.text_output

    # The agent is built first, so that the header can name its tools.
    agent = case.agent()
    found = {**tools.witness_session_tools(session), **await tools.witness_agent_tools(agent)}
    scenario = _header(case, found)

    declared, effective = _declared_session(case), _effective_session(session)
    for name in declared:
        if declared[name] != effective[name]:
            harness_errors.append(
                f"declared {name} = {declared[name]}, but the session holds {effective[name]}"
            )
    ledger.append(
        "harness",
        "run.start",
        case=case.name,
        seed=seed,
        framework={"name": "livekit-agents", "version": lk.__version__},
        clock="virtual" if virtual else "real",
        scenario=scenario,
        contracts=case.contracts,
    )
    _tap(session, ledger, harness_errors)

    await session.start(agent)
    audio_in.push_silence(0.1)  # first frame starts the scripted user's clock
    if case.hangup is None:
        await stt.done
        await asyncio.sleep(case.drain_delay)
        with contextlib.suppress(RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(session.drain(), timeout=case.drain_timeout)
    else:
        await _hang_up(case, session, world_origin, ledger, witness, backend, harness_errors)
    ledger.append(
        "harness",
        "history.final",
        read_from="AgentSession.history",
        items=[summarize_item(i) for i in session.history.items],
    )
    # Some releases cannot close a session while one of its tools is stuck.
    await witness.cancel_running()
    await backend.cancel_in_flight()
    try:
        await asyncio.wait_for(session.aclose(), timeout=case.drain_timeout)
    except asyncio.TimeoutError:
        harness_errors.append(f"the session did not close within {case.drain_timeout} s")
    await sync.aclose()
    return sink


def versions() -> dict[str, str]:
    import platform

    return {
        "livekit-agents": lk.__version__,
        "livekit": rtc.__version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
    }


def run_case(
    case: Case, seed: int = 0, *, virtual: bool = True, require_valid: bool = True
) -> RunResult:
    """Run ``case`` once on a fresh loop. Raises ``InvalidRun``, which carries the result,
    if the run is not valid evidence, unless ``require_valid`` is false. ``virtual=False``
    runs on the real clock as a negative control: such a run is never valid."""
    if virtual:
        result = run_virtual(lambda: _run(case, seed))
    else:
        result = asyncio.run(asyncio.wait_for(_run(case, seed), timeout=REAL_CLOCK_TIMEOUT))
    if result.invalid and require_valid:
        raise InvalidRun(result.invalid, result)
    return result


__all__ = [
    "DELAYS",
    "Case",
    "Delay",
    "Fault",
    "Intent",
    "InvalidRun",
    "RunResult",
    "run_case",
    "versions",
]
