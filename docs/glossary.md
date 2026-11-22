# Glossary

The terms Earful's pages, its command output and its reports use, each defined once. The
last column links to where the term is explained.

## A scenario

| Term | Meaning | Explained in |
| --- | --- | --- |
| scenario | One test conversation: its code, which is the agent and the scripted model, and the data it runs with by default. Named `module:function` when a function at the top level of a module returns it | [`testing.md`](testing.md#6-where-a-scenario-lives) |
| case | A scenario's data: the speeches, the hangup, the delays, the faults, the settings, the contract policy values and the seed. As a file it is an `earful.case/1` document. In Python, a `Case` object holds the code and the data together | [`search.md`](search.md#1-a-case-is-data-earfulcase1) |
| as written | The scenario with its author's values, before a search or a reduction changes anything. Also called the case as given | [`search.md`](search.md#5-the-shrinker) |
| inline | A scenario built in the process that runs it, with no `module:function` name. Its cases cannot be run by a worker process or kept as a bundle | [`testing.md`](testing.md#6-where-a-scenario-lives) |
| seed | The seed of every random choice in a run. The same case and seed give the same ledger | [`search.md`](search.md#1-a-case-is-data-earfulcase1) |
| origin | The first audio frame the scripted providers receive. The times of a scenario count from it | [`ledger.md`](ledger.md#time) |
| policy | The scripted model: a function from the context the framework shows the model to a reply. A contract's policy values are another thing: the tolerances a case declares for that contract | [`testing.md`](testing.md#2-the-model) |
| intent | A logical action the user wants done, such as one booking, with the number of durable effects the scenario allows for it | [`contracts.md`](contracts.md#4-what-the-test-author-declares) |
| effect | One durable write to Earful's backend for an intent. A tool declared an effect makes that write in place of its body | [`testing.md`](testing.md#3-side-effects) |
| claim | A span of a scripted reply that tells the user an intent succeeded | [`contracts.md`](contracts.md#a3-truthful-confirmation) |
| backchannel | A user speech that acknowledges and does not take the floor | [`contracts.md`](contracts.md#4-what-the-test-author-declares) |
| delay | How long one model call, one first audio or one backend write takes, declared as data | [`search.md`](search.md#1-a-case-is-data-earfulcase1) |
| fault | What a backend does to one write: `backend.error` rejects it, `backend.timeout` commits it and tells the caller it timed out, `backend.drop` loses it | [`search.md`](search.md#faults) |
| `commit first` | A `backend.timeout` in which the write commits before its caller is told it timed out | [`search.md`](search.md#faults) |
| `commit later` | A `backend.timeout` in which the caller is told first and the write commits afterwards | [`search.md`](search.md#faults) |
| hangup | The moment the user leaves. The session is closed then | [`search.md`](search.md#hangup) |

## A run

| Term | Meaning | Explained in |
| --- | --- | --- |
| ledger | The complete record of one run, one event for each line. The checker reads nothing else | [`ledger.md`](ledger.md) |
| ledger hash | SHA-256 of the normalized ledger. Two runs in which the same things happened have the same hash | [`ledger.md`](ledger.md#hashing) |
| virtual clock | The clock a run is on. It jumps to the event loop's next timer when the loop has nothing else to do, so a run takes no real time and repeats exactly | [`compatibility.md`](compatibility.md#the-virtual-clock) |
| witness | The part of Earful that observed an event: `world`, `sink`, `backend`, `llm`, `framework` or `harness` | [`contracts.md`](contracts.md#21-witnesses) |
| witnessed tool | A tool whose calls Earful records | [`ledger.md`](ledger.md#world-synthesis-and-tools) |
| valid evidence | A run that may produce verdicts. An invalid run did not pass and did not fail | [`ledger.md`](ledger.md#validity) |
| harness error | An error in Earful's own simulation or recording, or in the scenario's script. One makes the run invalid | [`ledger.md`](ledger.md#validity) |
| played | A word is played when its last audio sample has been consumed by the output | [`contracts.md`](contracts.md#3-played-audio) |
| playout segment | The audio the output plays between two flushes or clears | [`ledger.md`](ledger.md#identifiers) |

## Verdicts

| Term | Meaning | Explained in |
| --- | --- | --- |
| contract | A statement about one run that is decided from the run's ledger alone. There are seven | [`contracts.md`](contracts.md#1-what-a-contract-is) |
| instance | What a contract is checked for once: an intent, a reply, a user turn | [`contracts.md`](contracts.md#5-verdicts) |
| verdict | The outcome of one contract for one instance: pass, fail, not exercised or unobservable | [`contracts.md`](contracts.md#5-verdicts) |
| unobservable | Evidence the rule needs was lost, so neither a pass nor a fail can be shown. It carries a code and fails the run unless allowed | [`contracts.md`](contracts.md#5-verdicts) |
| not exercised | The instance, or the whole contract, had nothing to judge. It did not pass | [`contracts.md`](contracts.md#5-verdicts) |
| code | Why a verdict was not judged, such as `run-ended` (nothing to judge) or `outside-backend` (evidence lost) | [`contracts.md`](contracts.md#5-verdicts) |
| rule | A named part of a contract that a failed verdict cites, written `CONTRACT/RULE`, as in `M2/rule d` | [`testing.md`](testing.md#4-one-run) |
| finding | One reason a verdict is not a plain pass: the rule that broke, with its detail | [`contracts.md`](contracts.md#5-verdicts) |
| flag | A qualification on a verdict, such as `reading_split` | [`contracts.md`](contracts.md#5-verdicts) |
| reading | One way to say which of several replies with the same words a synthesized text was. The checker reports the reading most favourable to the system under test | [`contracts.md`](contracts.md#25-when-the-ledger-allows-more-than-one-reading) |
| predicate | A failure named by its contract and rule, with the instance left out. Two runs fail the same way when they break the same predicate | [`search.md`](search.md#2-the-same-failure-a-predicate) |
| evidence | Under pytest: the ledgers, cases and reports the `earful` fixture writes for a test that fails, under `.earful/` | [`testing.md`](testing.md#8-with-pytest) |

## The search

| Term | Meaning | Explained in |
| --- | --- | --- |
| variable | One time of a case that can be moved on its own, such as `speech.position 1` or `backend.latency 2` | [`search.md`](search.md#variables) |
| transition | An instant at which the run changed state: an event was recorded, a token or a word arrived, a turn could be committed | [`search.md`](search.md#3-the-boundary-scheduler) |
| handle | A moment at which a variable acts on the system, such as the instant a speech has lasted long enough to interrupt | [`search.md`](search.md#3-the-boundary-scheduler) |
| candidate | One case a search tries: the case as written with one handle put beside one transition, or with one fault added | [`search.md`](search.md#3-the-boundary-scheduler) |
| placement | One way to put one handle beside one transition. Placements that give the same case are one candidate | [`search.md`](search.md#3-the-boundary-scheduler) |
| judged | A case whose run was valid evidence and was checked | [`search.md`](search.md#2-the-same-failure-a-predicate) |
| not judged | A case that was refused, raised or gave a run that is not valid evidence. Nothing is concluded from it | [`search.md`](search.md#what-a-search-reports) |
| refused | A case the adapter declines before anything runs | [`search.md`](search.md#2-the-same-failure-a-predicate) |
| `impossible` | A placement for which no case exists, such as a delay below zero | [`search.md`](search.md#what-a-search-reports) |
| reached | A kind of transition a handle was put beside, in a run that could be judged | [`search.md`](search.md#what-a-search-reports) |
| `everywhere` | A rule that breaks in the case as given and in every candidate that ran. It does not depend on timing | [`search.md`](search.md#what-a-search-reports) |
| budget | `max_runs`: the number of runs after which a search stops | [`search.md`](search.md#3-the-boundary-scheduler) |
| `complete` | A search whose budget was not spent before every case ran and every failure was measured | [`search.md`](search.md#3-the-boundary-scheduler) |
| `pairs` | The option that tries two changes at once: a fault or a stretched delay with a moved speech or hangup, or two faults | [`search.md`](search.md#3-the-boundary-scheduler) |
| stretched | A backend write or a model generation made long enough for a speech to interrupt it | [`search.md`](search.md#3-the-boundary-scheduler) |
| profile | A seeded redraw of every provider delay, under which the speeches are moved again | [`search.md`](search.md#3-the-boundary-scheduler) |
| worker | A process a search runs cases in. It builds the scenario again from its name | [`testing.md`](testing.md#6-where-a-scenario-lives) |

## Windows

| Term | Meaning | Explained in |
| --- | --- | --- |
| window | A stretch of values of one variable over which every run breaks the predicate | [`search.md`](search.md#4-windows) |
| `passes` | An end of a window beyond which a run does not break the rule. An edge of the failure | [`search.md`](search.md#4-windows) |
| `blocked` | An end beyond which no case exists or none could be judged. Where a run could not be judged, the window says why | [`search.md`](search.md#4-windows) |
| `reach` | An end at which the search gave up. The failure may hold further | [`search.md`](search.md#4-windows) |
| instant | A window with no width and a passing run a microsecond either side. It turns on the order of two same-instant events and is not counted | [`search.md`](search.md#4-windows) |

## Reduction

| Term | Meaning | Explained in |
| --- | --- | --- |
| reduction | Finding which of a failing case's changes the failure needs. The shrinker does it | [`search.md`](search.md#5-the-shrinker) |
| isolate | The first step: find a smallest set of differences from the author's scenario that still fails | [`search.md`](search.md#5-the-shrinker) |
| remove | The second step: drop the speeches, delays and faults the failure does not need | [`search.md`](search.md#5-the-shrinker) |
| settle | The third step: move each remaining time to a round value in the middle of its window | [`search.md`](search.md#5-the-shrinker) |
| 1-minimal | Undoing any one remaining difference makes the case pass. A smaller failing case may still exist | [`search.md`](search.md#5-the-shrinker) |

## Bundles and replays

| Term | Meaning | Explained in |
| --- | --- | --- |
| bundle | What is kept of a failure: the case as found, the reduced case, the ledger of each and a manifest | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| manifest | The file of a bundle that records what failed, on what, and the digest of every other file | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| `original` | The case as the search found it | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| `minimal` | The reduced case | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| replay | Running a bundle's reduced case again on the installed release | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| reproduced | Every run of a replay was judged and broke the rule | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| clean | Every run of a replay was judged and the rule's contract passed in each. A test of a fix asserts this | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| repeatable | Every run of a replay gave the same ledger | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| identical | The ledger of a replay is the one the bundle recorded | [`search.md`](search.md#6-the-bundle-earfulbundle1) |
| `scenario_digest` | SHA-256 of the source of the module that defines the scenario's function. A replay compares it to say whether that file changed | [`search.md`](search.md#6-the-bundle-earfulbundle1) |

## The real clock

| Term | Meaning | Explained in |
| --- | --- | --- |
| cross-check | One run of a case on the virtual clock compared with runs of it on the real clock | [`testing.md`](testing.md#10-on-the-real-clock) |
| drift | The largest difference in time between an event of a real-clock run and the same event of the virtual run | [`testing.md`](testing.md#10-on-the-real-clock) |
| tolerance | How far a real-clock run may be from the virtual one and still agree with it | [`testing.md`](testing.md#10-on-the-real-clock) |
