"""Run one scripted case against a real AgentSession and return its ledger."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import livekit.agents as lk
from livekit import rtc
from livekit.agents import utils

from ..core import backend as backends
from ..core.backend import FAULTS, BackendMode, Effect, EffectBackend
from ..core.checker.policy import parse_all
from ..core.ledger import Ledger, ledger_hash
from ..core.schema import validity
from ..core.timebase import to_seconds, to_us
from . import tools
from ._compat import TranscriptSynchronizer, io
from .clock import (
    WALL_CLOCK_EPOCH,
    ClockPatchReport,
    VirtualTimeLoop,
    now_us,
    patch_report,
    run_virtual,
)
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

# Session options a Case declares through its own fields. The ledger header states them
# and contract L2 computes its reference from them, so nothing else may set them.
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


class InvalidRun(RuntimeError):
    """The run finished but may not be used as evidence. ``result`` holds its ledger."""

    def __init__(self, reasons: list[str], result: RunResult) -> None:
        super().__init__("run is invalid: " + "; ".join(reasons))
        self.reasons = reasons
        self.result = result


@dataclass(frozen=True)
class Intent:
    """A logical action the user wants done, carried out by the named tools."""

    intent: str
    tools: tuple[str, ...] = ()
    authorized: int = 1  # durable effects the scenario allows for this intent


DELAYS = ("llm.ttft", "llm.duration", "tts.ttfb", "backend.latency")


@dataclass(frozen=True)
class Delay:
    """A provider delay the scenario declares, in place of what its code would ask for.

    ``of`` names the delay: the model's time to first token (``llm.ttft``) or its total
    generation time (``llm.duration``), the voice's time to first audio (``tts.ttfb``), or
    the time the backend takes to commit a write (``backend.latency``). ``nth`` is the
    occurrence, from 1: the model call, the synthesized utterance or the backend request.

    The agent and the model policy are code and choose their own delays. Declaring one
    here makes it data: something a search can move and a reduction can drop.
    """

    of: str
    nth: int
    seconds: float


@dataclass(frozen=True)
class Fault:
    """Something the scenario declares goes wrong, for one occurrence of something.

    ``backend.error``: the backend rejects write ``nth``; nothing commits and the tool is
    told. ``backend.timeout``: the tool stops waiting for write ``nth`` after ``seconds``
    and is told it timed out, while the write still commits at its own latency.
    ``backend.drop``: write ``nth`` is lost; nothing commits and the tool is told, after
    ``seconds``, that it timed out.
    """

    of: str
    nth: int
    seconds: float | None = None  # how long the caller waits; not used by backend.error


@dataclass
class Case:
    """Everything that defines a run. Same case + same seed must give the same ledger.

    ``agent`` builds the agent under test and takes no arguments: an agent class, or a
    function that constructs one with whatever stands in for the services it uses. It is
    called once for each run, so no state is carried from one run to the next.

    A durable side effect reaches the backend in one of two ways. Code the agent calls,
    usually the stand-in for a service, calls ``earful.core.backend.effect`` where the
    real service would make a write durable. Or ``effects`` declares the write for a tool
    as a whole, by tool name: a call of that tool then makes the write in place of the
    tool's body, which suits a tool that talks to its service directly. The intent of a
    declared effect need not be listed in ``intents``; it is one, carried out by that tool.
    """

    name: str
    agent: Callable[[], lk.Agent]
    speeches: list[UserSpeech]
    policy: Policy
    intents: list[Intent] = field(default_factory=list)
    # Tool name -> the durable write a call of that tool makes, in place of its body.
    effects: dict[str, Effect] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> policy values
    delays: list[Delay] = field(default_factory=list)  # provider delays declared as data
    faults: list[Fault] = field(default_factory=list)  # things declared to go wrong
    # When the user hangs up, in seconds from the origin, if they do. The session is then
    # closed, as a participant's disconnect closes it. No speech may end after it.
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
    # How long to wait for the session to finish what it is doing once the scenario is
    # over. A turn that never finishes must not hang the run: it is cut off, and recorded.
    drain_timeout: float = 30.0
    sink: type[ClockedSink] = ClockedSink  # a faulty output is a subclass
    # Further AgentSession options. The options a Case declares through its own fields
    # cannot be set here.
    session_kwargs: dict[str, Any] = field(default_factory=dict)

    def check(self) -> None:
        """Refuse a scenario that contradicts itself, before anything runs."""
        clash = sorted(_DECLARED_OPTIONS & self.session_kwargs.keys())
        if clash:
            raise ValueError(f"session_kwargs may not set {clash}: use the Case fields")
        parse_all(self.contracts)  # raises PolicyError, a ValueError, on anything undefined
        ordered = sorted(self.speeches, key=lambda s: s.start)
        for before, after in itertools.pairwise(ordered):
            if before.end > after.start:
                raise ValueError(f"the speeches at {before.start} s and {after.start} s overlap")
        seen: set[tuple[str, int]] = set()
        for delay in self.delays:
            if delay.of not in DELAYS:
                raise ValueError(f"unknown delay {delay.of!r}; choose from {list(DELAYS)}")
            if delay.nth < 1 or delay.seconds < 0:
                raise ValueError(
                    f"delay {delay.of} #{delay.nth}: "
                    "nth counts from 1 and a delay cannot be negative"
                )
            if (delay.of, delay.nth) in seen:
                raise ValueError(f"delay {delay.of} #{delay.nth} is declared more than once")
            seen.add((delay.of, delay.nth))
        faulted: set[int] = set()
        for fault in self.faults:
            if fault.of not in FAULTS:
                raise ValueError(f"unknown fault {fault.of!r}; choose from {list(FAULTS)}")
            waits = fault.of != "backend.error"
            if (
                fault.nth < 1
                or (waits and (fault.seconds is None or fault.seconds < 0))
                or (not waits and fault.seconds is not None)
            ):
                raise ValueError(
                    f"fault {fault.of} #{fault.nth}: nth counts from 1; a timeout or a drop says "
                    "how long the caller waits, and an error does not"
                )
            if fault.nth in faulted:
                raise ValueError(f"backend write #{fault.nth} has more than one fault")
            faulted.add(fault.nth)
        if self.hangup is not None:
            if self.hangup < 0 or not self.speeches:
                raise ValueError(
                    "the user hangs up after their last speech; a user who never speaks cannot"
                )
            late = [speech.start for speech in self.speeches if speech.end_us > to_us(self.hangup)]
            if late:
                raise ValueError(
                    f"the user hangs up at {self.hangup} s, "
                    f"before the speech at {late[0]} s is over"
                )
        names = [it.intent for it in self.intents]
        if len(set(names)) != len(names):
            raise ValueError("an intent is declared more than once")
        for speech in self.speeches:
            undeclared = sorted(set(speech.retires) - set(names))
            if undeclared:
                raise ValueError(
                    f"speech at {speech.start} s retires undeclared intents {undeclared}"
                )


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
    """Turn-handling options, spelled the way this livekit-agents version expects."""
    kwargs: dict[str, Any] = {}
    if hasattr(lk, "TurnHandlingOptions"):
        kwargs["turn_handling"] = {
            # The default turn detector is an acoustic model; it cannot fire
            # deterministically on synthetic audio, so endpointing is VAD-based.
            "turn_detection": None,
            "endpointing": lk.EndpointingOptions(
                min_delay=case.min_endpointing_delay, max_delay=case.max_endpointing_delay
            ),
            "interruption": lk.InterruptionOptions(
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
    """The same options as the constructed session holds them, read through its public
    ``options``. The header states what was declared; this is how Earful knows the
    framework took it."""
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
    """The write each tool stands for: where the tool is defined, or in the scenario,
    which is used where both declare one. Raises ``ValueError`` if the scenario names a
    tool the agent does not have: a misspelt name would leave the tool's own body to run."""
    unknown = sorted(set(case.effects) - set(tools_found))
    if unknown:
        raise ValueError(
            f"effects are declared for {unknown}, which are not function tools of the agent "
            f"the run starts with; it has {sorted(tools_found)}"
        )
    at_the_tool = {name: found for name, found in tools_found.items() if found is not None}
    return {**at_the_tool, **case.effects}


def _intents(case: Case, effects: dict[str, Effect]) -> list[Intent]:
    """The scenario's intents, with those its declared effects imply: the intent of an
    effect is carried out by the effect's tool."""
    intents = {it.intent: it for it in case.intents}
    for tool, declared in sorted(effects.items()):
        known = intents.get(declared.intent, Intent(declared.intent))
        if tool not in known.tools:
            known = dataclasses.replace(known, tools=(*known.tools, tool))
        intents[declared.intent] = known
    return list(intents.values())


def _header(case: Case, tools_found: dict[str, Effect | None]) -> dict[str, Any]:
    """The scenario as declared, so the checker never has to import it."""
    effects = _effects(case, tools_found)
    header = _scenario(case, sorted(tools_found), _intents(case, effects))
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
    """Record public session events. Explanatory evidence, never ground truth.

    A handler must never raise into the framework: an exception here would change the
    behavior under test. Failures are collected and fail the run afterwards.
    """

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
    """The user leaves at the declared time, and the session is closed as a participant's
    disconnect closes it. Writes already requested still have their outcome."""
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
    # From here until the run is over, code the agent runs finds this run's witness and
    # backend: a tool that is called, a stand-in service that calls ``effect``.
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
    """Build the session and the agent, play the scenario through and close the session.
    Returns the sink, which holds what the output counted."""
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

    # The agent is built before the header is written, so the header can name the tools
    # the witness records. Nothing here reaches the ledger.
    agent = case.agent()
    scenario = _header(case, await tools.witness_agent_tools(agent))

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
    # Framework state read back by the harness: a judged record, never a reference.
    ledger.append(
        "harness",
        "history.final",
        read_from="AgentSession.history",
        items=[summarize_item(i) for i in session.history.items],
    )
    # The scenario is over. A tool still running is cut off and recorded as cancelled:
    # some releases cannot close a session while one of its tools is stuck.
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
    """Run ``case`` once on a fresh loop.

    Raises ``InvalidRun`` if the run may not be used as evidence (``RunResult.invalid``
    lists why), unless ``require_valid`` is false. The exception carries the result, so
    the ledger of an invalid run can still be inspected.

    ``virtual=False`` runs on the real clock with nothing patched. Such a run is never
    valid. It exists only as a negative control: it shows what the ledger hash does when
    timing is not controlled.
    """
    if virtual:
        result = run_virtual(lambda: _run(case, seed))
    else:
        result = asyncio.run(asyncio.wait_for(_run(case, seed), timeout=REAL_CLOCK_TIMEOUT))
    if result.invalid and require_valid:
        raise InvalidRun(result.invalid, result)
    return result
