# Voice application and incident measurements

Earful judged scenarios for seven of the twenty selected applications. Eleven other applications reached a refusal in the runner or the first-test generator. Two pinned unsupported framework releases. The three first tests that `earful init` generated each passed both their conversation and their search.

Of sixty issues screened, ten met the original scope rules. One incident met the reproduction criterion. On LiveKit #8477, the same reduced case and L1 check failed on 1.6.6 and passed on 1.8.13. Pipecat #6841 produced a repeatable failure near a mute transition. The report supplied neither the exact timings nor a fixed release for comparison.

## Method

The comparison keeps the repositories, issue snapshots and selection order from the sample selected at `848e8dd`. LiveKit contributed six official examples in repository or README order and six qualifying third-party agents in star order. Pipecat contributed four official examples covering function calling, a conventional bot, realtime and Flows, and the four highest-ranked qualifying third-party applications. No blocked entry was replaced.

The application runs used Python 3.13.12 on macOS arm64, a scratch home and an empty inherited environment. Examples that validate credential formats received dummy values. LiveKit ran on 1.8.13. The four official Pipecat applications ran on both 1.16.0 and 1.17.0. For the examples from the main Pipecat repository, the corresponding checkouts were `7cf75afc37f3` and `8c78a5403870`. Repository revisions for the other applications are in the table.

The fixtures exercised real tools, agent hooks or processors where indicated. Each judged result described the scripted services and whether Earful used its standard pipeline or the case's pipeline factory. The applications' production entrypoints were outside those runs. The two named tool waits below were the only LiveKit policy changes. Existing cases used the default scripted session.

## Applications

| Application at its measured revision | Runtime evidence | First test |
| --- | --- | --- |
| O1 · livekit-examples/agent-starter-python · `381ac11180` | Refused the agent's own language model. | Same refusal before writing. |
| O2 · livekit-examples/outbound-caller-python · `608f4c7055` | Confirmation and availability ran real tool bodies. L1 passed with a 3 s wait declared for `look_up_availability`. A separate transfer attempt named `transfer_call` and its missing-job-context error. | Generated, 2 passed. |
| O3 · livekit-examples/gemini-playground · `464ba79caa` | Refused the Google `RealtimeModel` wired to `PlaygroundAgent`. | Same model named before writing. |
| O4 · livekit/agents, homepage/livekit-expert · `9ce5a1e151` | Refused the agent's own language model. | Same refusal before writing. |
| O5 · livekit/agents, warm-transfer · `9ce5a1e151` | L1 passed with a 5 s wait declared for `transfer_to_human`. The stand-in completed. The real SIP tool raised a named `ToolError` without a room. | Generated, 2 passed. |
| O6 · livekit/agents, drive_thru · `9ce5a1e151` | Both order fixtures ran. Two repetitions of the same order had different ledger hashes because the application chooses a random response. | Refused construction with missing menu userdata. |
| T1 · ShayneP/local-voice-ai · `30d5ee17bb` | The multiplication tool ran. L1, M1 and M2 passed. | Generated, 2 passed. |
| T2 · YeJe-cpu/talk-to-fengge · `61f55d4209` | The real `Dev3Agent` constructor configured STT. Both the runner and setup refused its own recognizer. | Refused the agent’s own STT. |
| T3 · kirklandsig/AIReceptionist · `9ad68a0ab2` | Loaded `receptionist.agent` from the repository root and constructed the agent with its example dental config. Both paths named the OpenAI realtime model assigned before session construction. | Refused the same realtime model. |
| T4 · ngoanpv/DeepInterview · `bd6d136fff` | Both endpointing variants passed L1 and M2. M1 reported `handoff-context` for the word whose ownership the handoff obscured. | Refused construction with missing interview userdata. |
| T5 · blackdwarftech/siphon · `6965cdd6e1` | Imported the dental example with its Sarvam, Cartesia and OpenRouter plugins and dummy credentials. Its exported `agent` is a configured platform instance. | Refused the instance as an unsupported entry. |
| T6 · nickvasilescu/os1-her · `a3a7bc4e6c` | The real agent, constructed with stand-ins for the engine and bus, reached the runner and was refused for supplying its own voice. | Refused the default constructor because it needs `engine.work`. |
| P1 · pipecat-ai/pipecat, 07-function-calling | A wrapper called the application's actual weather function. The result identified the pipeline used. The application's Silero VAD and the default turn model each received an explicit refusal. | Identified the hyphenated module name. |
| P2 · pipecat-ai/pipecat-examples, simple-chatbot · `9a0b4b981762` | The application processor ran in the supplied pipeline. The standard-pipeline control identified its different scope. | Identified the hyphenated module name. |
| P3 · pipecat-ai/pipecat-examples, word-wrangler · `9a0b4b981762` | Refused the application's `GeminiLiveLLMService` by name. | Identified the coroutine entry function. |
| P4 · pipecat-ai/pipecat, Flows food_ordering | The actual YAML flow hit the late-registration refusal. Pre-registering names hit the conflicting-handler refusal. A standalone node ran as a separate unit. | Identified the coroutine entry function. |
| P5 · dograh-hq/dograh · `b5e30bd65d75` | Source pins a fork based on Pipecat 1.12.0. Verified the version gate with stock 1.12.0. | Outside the supported range. The fork was inspected. |
| P6 · C4rohan/Lampline · `0c16a533f2b5` | Loaded the local `vk` package. The real `VoiceSession` built the app’s aggregator and pipeline using scripted transport and service ends. Its Silero VAD was refused. | Refused the session class as an unsupported entry. |
| P7 · Sallyn0225/eidolon-sc · `d139f2cf2890` | Executed the app’s `_LiveCall` pipeline constructor with offline transport and speech stand-ins. It routed through the conversation engine and lacked the required model endpoint. Earful refused the pipeline. | Refused the conversation engine class as an unsupported entry. |
| P8 · COSS-India/VoicEra · `3dd429756d71` | The application pins Pipecat 1.8.1. Verified the version gate on that release. | Outside the supported range. |

## Incident trials

The two archived GitHub searches yielded 493 distinct LiveKit issues and 328 Pipecat issues. We rechecked each newest-thirty list against its archived issue records. Scope requires a conversational symptom in the Python framework reachable through scripted participants. The first five eligible issues from each list were attempted. The LiveKit cases ran on 1.6.6 and 1.8.13, with additional 1.8.12 comparisons for #8614 and #8477. The Pipecat cases ran on both supported releases.

| Incident | Result and control |
| --- | --- |
| [LiveKit #8753](https://github.com/livekit/agents/issues/8753) | The short sound and silence-window control exercised the pause. The scripted VAD supplies the disputed timing calculation, and the contracts do not judge its correctness. |
| [LiveKit #8614](https://github.com/livekit/agents/issues/8614) | With preemptive generation, the user tag and tool payload lost their value on 1.8.12 and retained it on 1.8.13. The non-preemptive control retained it on both. The contracts passed in all four cases. |
| [LiveKit #8590](https://github.com/livekit/agents/issues/8590) | Dynamic endpointing was refused when requested on the agent. The session-options case explained the limits of the exposed fields. |
| [LiveKit #8477](https://github.com/livekit/agents/issues/8477) | **Reproduced the blocked next turn.** L1 milestone 1 failed after a rejected handoff on 1.6.6. The free-supervisor control passed, including with a fresh context. The same failure disappeared on 1.8.12 and 1.8.13. A separate M2 rule d failure reflects a LiveKit history defect triggered by the application reusing a busy agent. The queued-speech part of the report was outside this check. |
| [LiveKit #8469](https://github.com/livekit/agents/issues/8469) | Deliberately stalling playout broke L1 milestone 3 on both releases. Restoring playout passed. The check could not distinguish the reported stuck agent state. |
| [Pipecat #6905](https://github.com/pipecat-ai/pipecat/issues/6905) | Both stale and late manual-summary cases refused the external summary model before inference. Search refused the same case. The control that replaced history without a summary broke separate memory and reply rules. |
| [Pipecat #6841](https://github.com/pipecat-ai/pipecat/issues/6841) | **Symptom shown, cause unconfirmed.** The late-frame case broke L1 and nearby controls passed on both releases. Search found and replayed a reduced failure. Nothing in the incident established that the incoming frames had those timings. |
| [Pipecat #6819](https://github.com/pipecat-ai/pipecat/issues/6819) | The transform and missing-word-time variants lost audio attribution. The plain control passed. Earful's voice cannot execute the reported TTS markup path. |
| [Pipecat #6769](https://github.com/pipecat-ai/pipecat/issues/6769) | The application's replacement or second voice was refused. A simulated hold broke L2, and removing it passed. The provider reconnect itself remained untested. |
| [Pipecat #6694](https://github.com/pipecat-ai/pipecat/issues/6694) | The reported 1.15.0 release is outside the supported range. On both tested releases the callback and final context contained the same symbols. The contracts passed. |

### Search and replay evidence

| Baseline and reduced predicate | Search | Replay |
| --- | --- | --- |
| LiveKit 1.6.6, #8477, L1 milestone 1 | 63 candidates, all judged. The original broke L1 milestone 1 and M2 rule b. | Three replays matched the saved ledger hash. The same bundle passed its L1 predicate on 1.8.13. |
| LiveKit 1.8.13, #8477, M2 rule d | The rejected transfer lost the recorded function call and result. | All three replays of the reduced case matched the saved ledger hash and failed only M2 rule d. |
| Pipecat 1.16.0, #6841, L1 milestone 1 | 463 candidates, 449 judged and 14 invalid. The original case passed. A nearby case broke the predicate. | Three replays matched the saved ledger hash. This establishes repeatability of the generated timing. |
| LiveKit 1.5.6, historical #3702 and #5509 | Used the two pre-existing bundles. | Both reproduced with the original ledger bytes. |

### Why the rejected handoff still breaks M2 on LiveKit 1.8.13

Two native `AgentSession` instances and LiveKit’s own test model reproduced the history loss independently of Earful. The SDK emitted `function_tools_executed` with the transfer call ID in both trials. When the target was already running on another session, the caller answered the next user turn, but its public `session.history.items` held neither the function call nor the result. When the target was free, that history held both entries with the same ID.

On this release, `AgentActivity._pipeline_reply_task_impl` emits the execution event, calls `update_agent`, then commits the call and result. The busy-target exception skips the commit. Earful read that same public history. The L1 finding on older releases concerned the next turn, and the M2 finding concerns this missing pair. [LiveKit implementation](https://github.com/livekit/agents/blob/9ce5a1e151/livekit-agents/livekit/agents/voice/agent_activity.py)

### Issues screened out

Each number below occurred in its framework's frozen set of thirty. The five eligible reports for each framework appear above.

| Outside the modeled conversation | LiveKit | Pipecat |
| --- | --- | --- |
| Provider, realtime or model-specific behavior | 8737, 8704, 8702, 8662, 8637, 8636, 8622, 8596, 8585, 8567, 8512, 8489 | 6901, 6897, 6896, 6887, 6884, 6849, 6770, 6743 |
| Transport, room, process or resource management | 8606, 8496 | 6985, 6959, 6874, 6872, 6827, 6806 |
| Feature request or question | 8679, 8611, 8595, 8578, 8574, 8536, 8535, 8523, 8514, 8503 | 6956, 6868, 6823, 6787, 6768, 6744, 6732, 6698 |
| Event, helper or test tool without the reported symptom in the modeled conversation | 8723 | 6955, 6928, 6864 |

## Regression checks

All 28 committed reference ledgers matched over 715 runs. Each of the five new Pipecat 1.17.0 references was run 100 times in-process and 20 times in fresh processes. Each of the 23 existing references was run three times in-process and twice in fresh processes. Every case produced one distinct normalized ledger hash on its pinned release.

The wheel quickstart ran in a fresh project. It found a second booking, wrote the offline report, kept and replayed the reduced case, then verified that adding the idempotency key fixed it. It also generated and ran a first test with an explicitly declared write. The separate wheel smoke test covered installation, the command and the pytest plugin without a framework installed.
