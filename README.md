# LearnFlow Recovery — milestones 1–6

A local, synthetic learning platform for an AI hackathon. This milestone provides
the backend evidence-to-progress pipeline, reproducible developer scenarios,
diagnostics, verification, controlled recovery, and an opt-in OpenAI investigation
agent, a persisted learner-view cache, and a local React/TypeScript dashboard and
recovery console. All students are synthetic. Only opted-in agent
investigations use an external service; the backend and routine tests run locally.

## Setup and run

Use Python 3.11 or newer. From the project directory, on Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m learnflow.seed
.\.venv\Scripts\python.exe -m uvicorn learnflow.app:create_app --factory --reload
```

On macOS/Linux use `python3 -m venv .venv` and `.venv/bin/python` in place
of `.\.venv\Scripts\python.exe`. Open http://127.0.0.1:8000/docs for the
interactive API. The app initializes tables but does not silently seed or reset.

The default database is `learnflow.sqlite3` in the working directory. To use
another file, seed it with `python -m learnflow.seed --db path.sqlite3` and set
`LEARNFLOW_DB` to that same path before starting the server.

## Repeatable reset

Stop the server and rerun `python -m learnflow.seed` (using your environment's
Python). This **deletes all evidence and progress in the selected database** and
recreates one course, three lessons, and three synthetic students. Repeated runs
produce the same initial state. Student IDs and lesson IDs are each 1–3.

## Data flow and rules

1. `POST /events` stores a `lesson_completed` activity event. Supply
   `{"student_id":1,"lesson_id":1}`. It does not update progress.
2. `POST /quiz-results` accepts the same IDs plus `"score":90`. It stores an
   immutable-through-the-API quiz result and a linked `quiz_recorded` event in
   one transaction. The response includes `event_id`.
3. Call `POST /events/{event_id}/process` for **both** events. This marks the
   event processed and recalculates that student's derived progress in one
   SQLite write transaction. Only processed evidence participates. A separate,
   audited step then refreshes the learner-view cache in the same transaction.
4. A lesson completes when a completion event and any quiz attempt scoring at
   least 70 are processed, and all preceding lessons are complete. Completion
   unlocks the next lesson. All three complete the course.

Evidence may arrive or be processed out of order. Evidence for a locked lesson
is retained, but the lesson cannot complete until its prerequisites complete.
When prerequisites complete, already processed evidence is applied automatically.
A later failed attempt does not revoke a previous pass. Eligibility means the
prerequisites are satisfied, so completed lessons remain eligible.

Reprocessing an event preserves its original processing timestamp and recalculates
the same projection without counters or duplicate rows. The response reports
`already_processed`. Atomic processing serializes writers. Repeated ingestion
creates separate evidence records; ingestion request deduplication is not included.

## Inspect the pipeline

| Endpoint | Purpose |
| --- | --- |
| `GET /students` | Synthetic student IDs |
| `GET /lessons` | Course, lesson ordering, passing thresholds |
| `GET /quiz-results` | Source quiz attempts, including failures |
| `GET /events` | Stored events, linked quiz IDs, processing timestamps |
| `GET /students/{student_id}/progress` | Completion evidence flags, quiz pass flags, eligibility, course completion |
| `GET /students/{student_id}/courses/{course_id}/learner-view` | Persisted learner-facing snapshot and freshness metadata; never recalculates or refreshes on read |

There are no API endpoints to edit/delete source evidence or directly edit progress.
Unknown IDs return 404; scores outside 0–100 return 422.

## Milestone 2: developer scenarios

These commands run locally, with no server required. Each `setup` destroys and
reseeds the **entire selected database**, then creates the scenario for student 1,
course 1. Use a dedicated demo database and stop any server using it first.
The scenario runner is developer-only and is not imported by application or
recovery tools. Labels are not stored in the database, logs, or tool responses.

Exact PowerShell setup commands for the three scenarios:

```powershell
.\.venv\Scripts\python.exe -m learnflow.scenarios setup missed-processing --db demo.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios setup partial-update --db demo.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios setup healthy-control --db demo.sqlite3
```

Run one setup at a time; each replaces the preceding scenario. Repeating a setup
recreates the same logical state and IDs; creation/processing timestamps reflect
the current run. Both evidence records exist in every scenario.

| Developer scenario | Observable state before recovery |
| --- | --- |
| `missed-processing` | Lesson 1 has completion and score 90 evidence; neither event is processed; stored completion is false and lesson 2 remains locked. |
| `partial-update` | Lesson 1 is earned and processed; stored lesson 2 eligibility was left false. Eligibility is persisted in this architecture, so this fault needs no architecture change. |
| `healthy-control` | Processed completion and score 40 evidence; lesson 1 is incomplete and lesson 2 is correctly locked. |

Reset commands for each scenario return the database to the clean, seeded state
(without injected evidence). To recreate a fault after reset, rerun its setup:

```powershell
.\.venv\Scripts\python.exe -m learnflow.scenarios reset missed-processing --db demo.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios reset partial-update --db demo.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios reset healthy-control --db demo.sqlite3
```

## Inspection, verification, and recovery

After setting up any scenario, run:

```powershell
.\.venv\Scripts\python.exe -m learnflow.tools inspect --db demo.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify --db demo.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools recover --db demo.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify --db demo.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools inspect --db demo.sqlite3 --student 1 --course 1
```

All commands return structured JSON. `inspect` reads a consistent snapshot of
events, quizzes, requirements with explicit prerequisites, progress, replay
receipts, and available processing/recovery history. It does not expose scenario
metadata. `verify` is read-only: it compares each stored progress field against
the state supported by **all existing evidence**, returning `consistent` and
`mismatches` with lesson ID, field, actual, and expected values. Unprocessed
evidence can therefore produce a mismatch even though deferred processing is a
valid intermediate milestone 1 state. Expected values are computed from public
evidence and course rules, never hidden scenario answers.

`recover` explicitly replays one student's one course. It inserts unique replay
receipts for existing events and runs the same deterministic projection used by
ordinary event processing. It changes no source event or quiz row, including
`processed_at`. Diagnostics show replay receipts separately so a null original
processing timestamp is not mistaken for an unreplayed event. Ordinary processing
also recognizes these receipts, preserving recovery results on later processing.
Only the selected course's progress is rewritten; no evidence is invented.

Each recovery attempt appends an audit record with student/course, timestamp,
before/after progress, success or error, and a sanitized error code. A successful
no-op still records an attempt; it does not duplicate receipts or earned progress.
Recoverable operation failures roll back progress and receipts before recording
the failed attempt. Database errors never include raw exception messages or SQL
in the audit. A database that cannot be opened, locked for writing, or written to
cannot persist an audit; callers must handle that infrastructure failure.
Ordinary successful event processing is also logged. Earlier milestone 1
operations have no retrospective logs; original event timestamps remain available.

The Python interface is `RecoveryTools(db_path)` in `learnflow.tools`, with
`read_activity_events`, `read_quiz_results`, `read_requirements`, `read_progress`,
`read_processing_history`, `inspect`, `verify`, `recover`, `inspect_view`,
`refresh_view`, and `verify_end_to_end`. Methods accept only
student/course IDs; the trusted host binds the database path. There is no generic
SQL or source-write tool. These are local functions/CLI tools, not new HTTP routes.
The existing simulator API stays available for synthetic evidence ingestion.

New tables are created additively by application startup or seed/scenario setup.
For a preserved milestone 1 database, stop the server and initialize the schema
without resetting its data before using the tools:

```powershell
.\.venv\Scripts\python.exe -c "from learnflow.db import initialize; initialize('learnflow.sqlite3')"
```

Do not register the scenario runner, seed/reset, simulator ingestion, test files,
or arbitrary filesystem/SQL access for the future recovery agent. The local
Python process itself is not a security sandbox; the next milestone must enforce
that tool allowlist and filesystem/database permissions in its host. Milestone 3
now enforces the model tool allowlist and binds scope in application code; it does
not sandbox a human running Python or expose filesystem access to the model.

## Milestone 3: configuration

Update dependencies, then copy the example only if `.env` does not already exist:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` **locally in your editor**. Replace `OPENAI_API_KEY` with your own key
and `OPENAI_MODEL` with `gpt-5.4-mini` or another accessible Responses model
supporting function calling and structured outputs. Do not paste keys into chat,
source code, commands, screenshots, or Git. `.env` and `.env.*` are ignored;
`.env.example` has placeholders and optional budget defaults only. No key is
provided or created. Environment variables take precedence over the selected
local file; there is no parent-directory search or variable interpolation.

As checked on October 31, 2026, official documentation lists
[`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) as an
economical model supporting Responses, function calling, and structured outputs.
Access and billing depend on your OpenAI project. `OPENAI_MODEL` is required;
the agent does not hardcode a model.

| Optional environment setting | Default | Range |
| --- | --- | --- |
| `LEARNFLOW_MAX_TURNS` | 10 | 1–50 model requests |
| `LEARNFLOW_MAX_TOOL_CALLS` | 20 | 1–100 requested tools, including rejected calls |
| `LEARNFLOW_MAX_REPAIRS` | 2 | 0–5 total progress-recovery/cache-refresh attempts |
| `LEARNFLOW_REQUEST_TIMEOUT` | 30 | More than 0, at most 120 seconds |
| `LEARNFLOW_MAX_OUTPUT_TOKENS` | 4096 | 256–16384 per response, including reasoning tokens |

SDK retries are disabled. These ceilings are not a guaranteed dollar budget.
The timeout is the SDK network timeout, not an investigation-wide wall-clock
deadline. SQLite retains its bounded lock wait. Keep limits low for first checks.

## Milestone 3: run and inspect an investigation

No server is required. Choose **one** developer setup (each destroys and replaces
the entire selected demo database):

```powershell
.\.venv\Scripts\python.exe -m learnflow.scenarios setup missed-processing --db demo.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios setup partial-update --db demo.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios setup healthy-control --db demo.sqlite3
```

After one setup, run a live, potentially paid investigation. Supply the reported
symptom without passing any scenario label or expected answer to the agent:

```powershell
.\.venv\Scripts\python.exe -m learnflow.agent_cli investigate --allow-paid --db demo.sqlite3 --student 1 --course 1 --symptom "My next lesson is locked. Please investigate the available evidence."
```

The command prints a structured report and `.traces/<run-id>.jsonl` path. Replace
`<run-id>` with its actual printed value to inspect the trace; inspect resulting
progress and milestone 2 recovery audits using the existing tools:

```powershell
.\.venv\Scripts\python.exe -m learnflow.agent_cli trace --file .traces/<run-id>.jsonl
.\.venv\Scripts\python.exe -m learnflow.tools inspect --db demo.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify --db demo.sqlite3 --student 1 --course 1
```

`--trace-file` optionally selects a new trace path; existing files are never
overwritten. `--env-file` selects another local configuration file. Use `--db`
explicitly for controlled checks (the default is the process `LEARNFLOW_DB` or
`learnflow.sqlite3`). Trace reading is offline.

The next small **opt-in live smoke check** can be run with:

```powershell
.\.venv\Scripts\python.exe -m learnflow.scenarios setup missed-processing --db smoke.sqlite3
.\.venv\Scripts\python.exe -m learnflow.agent_cli live-smoke --allow-paid --db smoke.sqlite3 --student 1 --course 1 --symptom "Please investigate why my next lesson is locked."
.\.venv\Scripts\python.exe -m learnflow.tools verify --db smoke.sqlite3 --student 1 --course 1
```

`live-smoke` is an explicitly opted-in alias for the same bounded investigation,
not a canned success test: it neither injects faults nor knows expected answers.
**No paid live calls were run during implementation.** Mocked tests establish
orchestration behavior, not live judgment, cost, latency, or account/model access.
Exit code 0 indicates supported `recovered`/`no_fault_found`; escalation or execution
failure returns 1; CLI argument errors (including missing `--allow-paid`) return 2.

## Milestone 3: workflow, safety, report, and trace

`agent.py` owns provider-independent orchestration. `openai_provider.py` alone
uses the official SDK and Responses API following OpenAI's
[function-calling guide](https://developers.openai.com/api/docs/guides/function-calling)
and [structured-output guide](https://developers.openai.com/api/docs/guides/structured-outputs).
It sends function outputs back by call ID and keeps response items in memory
for multi-turn continuity. With `store=False`, opaque reasoning continuation is
carried only in memory and never logged. No hidden chain-of-thought is requested.
`store=False` does not override applicable provider data policies: opted-in runs
send the symptom and scoped tool results to OpenAI.

The real model chooses its investigations and repair requests. There are no
scenario-to-action mappings. Only `inspect`, `verify`, `recover`, `inspect_view`,
`refresh_view`, and `verify_end_to_end` are exposed.
Every function takes exactly `{}`, using a strict schema validated locally.
Student/course/database scope is bound by the host outside model control. No
tool exposes source writes, SQL, shell, source browsing, scenarios, test answers,
credentials, repository uploads, or database uploads. All tool calls execute
sequentially, even if a model returns a batch despite parallel calls being disabled.
Symptoms and strings in diagnostic/log results are untrusted data.

Progress recovery requires inspection and progress mismatches. Cache refresh
requires cache inspection and correct progress but a stale/missing cache. Both
operations share the configured repair budget.
Invalid arguments, unknown tools and tool failures return structured errors
within the bounded loop. API errors or incomplete responses stop safely; exhausted
budgets escalate without inferring success. Recovery continues to use milestone 2
logic and its existing audit trail, preserving source rows and unrelated scope.

The final report contains status (`recovered`, `no_fault_found`, `escalated`, or
`execution_failed`), evidence references, concise diagnosis, host-recorded actions
and audit IDs, verification result, remaining limitations, and recommended next
steps. Evidence references identify actual trace tool results (e.g. `tool-0001`).
For CLI investigations, `recovered` requires successful changed repair(s), subsequent
passing end-to-end verification, and references to source/cache inspections,
all changed repairs and latest verification. `no_fault_found` requires inspections,
passing end-to-end verification and no dispatched repair. Unsupported success claims are
downgraded and fabricated references rejected. Host actions and verification fields
never come from model assertions. Diagnosis prose remains model-generated and
should be reviewed against the evidence. A failed quiz never becomes earned completion.

`reported_symptom_resolved` is always `null`: consistency does not establish that
an external complaint (browser display, notifications, etc.) was resolved. The
report always states this observability limitation, even after a valid recovery.

Append-only JSONL traces contain scope, validated arguments, tool names/results,
error codes, timings, token usage when available, and final status. Intent is
flushed before mutations. Raw invalid arguments, prompts, API errors, keys, and
hidden reasoning are not recorded. Configured credentials and API-key patterns
are redacted from sent symptoms, tool results, reports and traces. Default traces
are ignored by Git. Recovery audit IDs connect trace actions to SQLite records.

Traces and database audits are not an atomic cross-file transaction or tamper-proof
records. If trace writing fails after a repair, inspect the audit and verify the
database; the CLI reports execution failure. A crash may leave an intent without
a result. Verification is a point-in-time snapshot; concurrent changes can make
it stale. The host provides tool isolation, not a sandbox for arbitrary local Python.

## Milestone 4: learner-view architecture and initialization

The pipeline is `events + quiz_results -> progress -> learner_views`. Source
evidence remains authoritative. The cache is a snapshot of **stored derived
progress**, not another implementation of course rules. Each student/course
snapshot stores lesson flags, course completion, a version, SHA-256 content
fingerprint, and `refreshed_at`. The fingerprint describes copied content; it is
not a security signature or an expiration deadline.

Seed/reset initializes baseline views at version 1 for all seeded students.
Ingestion changes neither progress nor cache. Normal event processing explicitly
publishes after computing progress, with separate `process_event` and
`refresh_learner_view` history records in one SQLite transaction. A cache write
failure rolls back the entire operation. Fault injection stays developer-only.

`recover` remains progress-only and never refreshes the cache. `refresh_view`
copies current progress without processing evidence or recalculating eligibility,
even if progress is wrong. Manual refresh before progress recovery can therefore
publish incorrect progress. The agent prevents that ordering: correct progress
first, then refresh only if still needed. Refreshes/no-ops have before/after
audits; failed writes roll back and log sanitized errors. Changed snapshots
increment version. Identical copies preserve version, fingerprint, and timestamp,
while still recording an audit attempt.

Application startup or `initialize` adds the table without resetting or backfilling
existing data. Missing caches read as `initialized:false`, version 0, empty lessons,
and unknown course completion. Reads never populate them. Explicit processing or
`refresh_view` creates version 1. Upgrade a selected old database without resetting:

```powershell
.\.venv\Scripts\python.exe -c "from learnflow.db import initialize; initialize('learnflow.sqlite3')"
.\.venv\Scripts\python.exe -m learnflow.tools verify_end_to_end --db learnflow.sqlite3 --student 1 --course 1
```

If progress is inconsistent, recover it first. If only the view is missing/stale,
refresh it explicitly. Substitute your intended path in both commands. Previous
smoke databases are not reset or upgraded automatically by this milestone.

## Milestone 4: scenarios and distinct checks

All developer setups target student 1, course 1. Each setup/reset destroys and
reseeds only the named database; use dedicated files. Scenario names and these
expected outcomes are developer documentation, never agent inputs.

| Scenario | Evidence vs progress | Progress vs learner view | Justified operations |
| --- | --- | --- | --- |
| `missed-processing` | Valid completion/score 90 evidence is unprocessed | Initially matching baseline; stale after recovery | Recover progress, then refresh view |
| `partial-update` | Lesson 1 earned; lesson 2 eligibility incorrectly 0 | Previously published correct view differs from broken progress | Recover progress only; view then matches |
| `healthy-control` | Correct failed-quiz state: score 40 vs threshold 70 | Correct published failed-quiz state | Neither; lesson 2 stays locked |
| `stale-view` | Correct earned lesson 1 and unlocked lesson 2 | Old baseline shows incomplete/locked | Refresh view only |
| `combined` | Lesson 1 completion and lesson 2 eligibility incorrectly 0 despite processed valid evidence | Snapshot shows lesson 1 complete but lesson 2 locked | Recover progress, verify, refresh view, verify again |

New deterministic setup/reset commands:

```powershell
.\.venv\Scripts\python.exe -m learnflow.scenarios setup stale-view --db milestone4-stale.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios setup combined --db milestone4-combined.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios reset stale-view --db milestone4-stale.sqlite3
.\.venv\Scripts\python.exe -m learnflow.scenarios reset combined --db milestone4-combined.sqlite3
```

Run a setup when testing its scenario. Run reset only when intentionally returning
that dedicated file to clean seed state. Inspect and compare the layers using:

```powershell
.\.venv\Scripts\python.exe -m learnflow.tools inspect --db milestone4-combined.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools inspect_view --db milestone4-combined.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify --db milestone4-combined.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify_end_to_end --db milestone4-combined.sqlite3 --student 1 --course 1
```

`verify` preserves its existing progress-only response. `verify_end_to_end` returns
separate `progress` and `learner_view` checks with structured mismatches, overall
`consistent` (both must pass), and `browser_ui_verified:false`. Both checks read
one transaction snapshot and never repair. Cache mismatches identify lesson
fields, initialization, course completion, and fingerprint differences.

An entirely offline manual demonstration of combined recovery:

```powershell
.\.venv\Scripts\python.exe -m learnflow.scenarios setup combined --db milestone4-combined.sqlite3
.\.venv\Scripts\python.exe -m learnflow.tools recover --db milestone4-combined.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify_end_to_end --db milestone4-combined.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools refresh_view --db milestone4-combined.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.tools verify_end_to_end --db milestone4-combined.sqlite3 --student 1 --course 1
```

The first check has correct progress but an inconsistent cache. The last has both
consistent. For `stale-view`, skip progress recovery and only refresh. Publishing
before progress recovery merely copies wrong progress; it cannot fix source-vs-progress.

## Milestone 4: agent scope, budgets, and optional live check

Both CLI investigation commands require `verification_scope:learner_view`.
Reports include separate progress/cache outcomes, repair audit IDs,
`browser_ui_verified:false`, and `reported_symptom_resolved:null`. Progress-only
checks cannot support a successful CLI learner-path report. Every changed repair
must be cited; mutations invalidate previous verification. The model selects its
operations; healthy cases must remain mutation-free.

For direct Python callers, `Investigator` retains its milestone 3 default
`verification_scope='progress'`, and `Limits()` retains a single-repair default
for compatibility. Reports label that scope and disclaim cache verification.
New learner-path callers must pass `verification_scope='learner_view'` and
`Limits(max_repairs=2)` (or configuration-derived limits). The CLI does this
explicitly, with a runtime configuration default of two total mutations.

**Existing `.env` files are not rewritten.** If yours still sets
`LEARNFLOW_MAX_REPAIRS=1`, change it to 2 locally or override it in your PowerShell
session. One repair cannot handle both layers; exhausted budgets escalate rather
than invent success. The existing 10-turn/20-call bounds permit the combined
workflow and still cap retries.

No paid calls were run for milestone 4. To opt in yourself later:

```powershell
$env:LEARNFLOW_MAX_REPAIRS = "2"
.\.venv\Scripts\python.exe -m learnflow.scenarios setup combined --db milestone4-agent.sqlite3
.\.venv\Scripts\python.exe -m learnflow.agent_cli live-smoke --allow-paid --db milestone4-agent.sqlite3 --student 1 --course 1 --symptom "Please investigate why my next lesson is locked."
.\.venv\Scripts\python.exe -m learnflow.tools verify_end_to_end --db milestone4-agent.sqlite3 --student 1 --course 1
.\.venv\Scripts\python.exe -m learnflow.agent_cli trace --file .traces/<printed-run-id>.jsonl
```

Replace the trace placeholder with the printed path. To check cache-only selection,
use `setup stale-view` with another dedicated database, then investigate and verify
that file. These are opt-in paid development checks, not part of pytest or evidence
of general reliability.

## Controlled milestone 4 development smoke runner

`learnflow.development_smoke` prepares and checks `stale-view` followed by
`combined`. These are **labelled development smoke tests, not held-out evaluation**.
The runner reuses scenario setup, `RecoveryTools`, the existing Responses provider,
the real `Investigator` loop with learner-view scope, and JSONL tracing. It does
not implement another agent, force its choices, or repair anything outside that
agent. Labels and expected answers stay in the runner, never model/tool inputs.

From the project directory, using your existing locally configured model/key:

```powershell
$env:LEARNFLOW_MAX_REPAIRS = "2"
.\.venv\Scripts\python.exe -m learnflow.development_smoke --allow-paid
```

This command is **potentially paid**. Without `--allow-paid` it exits before loading
configuration, creating files, or starting an investigation. If the configured
repair budget is below two, preflight fails before spending on either case; it
does not silently rewrite `.env` or increase other budgets. An optional
`--env-file` selects local configuration, and `--output-root` changes the artifact
parent (default `.traces/milestone4`). Do not run paid smoke checks as routine tests.

Each explicit invocation reserves a unique new run directory. Each case gets a
separate `learning.sqlite3` inside its own new subdirectory. Existing databases,
directories and traces are never overwritten or reset. Each case captures:

- `before.json`: all source events and quiz results, unrelated students and their
  progress/cache/audit/receipt state, target progress/view, and both progress-only
  and end-to-end verification before the investigation.
- `agent.jsonl`: the normal redacted agent trace. Exactly one bounded investigation
  uses the symptom `Please investigate why my next lesson is locked.` The runner
  does not automatically retry the investigation; provider SDK retries remain off.
- `after.json`: fresh independent verification and state snapshots after the
  investigation, including when the agent reports an API/execution failure.
- `summary.json`: status, requested/successful repair tools, initial and final
  verification, source and unrelated-student preservation checks, lesson 2 checks,
  configured model name, available token totals, and elapsed time.

The run-level `summary.json` and console JSON combine both cases, mark skipped
cases, and identify artifact paths. `stale-view` must start with correct progress
and stale cache, request only `refresh_view`, and finish recovered/consistent.
`combined` must start with both checks failing, request `recover` followed by
`refresh_view`, and finish recovered/consistent. Both derived and cached lesson 2
must be eligible **and not completed**. Source evidence and unrelated students must
be identical before and after. Even a rejected unnecessary recovery request fails
the expected-tool-sequence check; successful final consistency alone is insufficient.

The runner stops after any failed case, including API/configuration errors or
unexpected initial state, and never repeats an investigation automatically. If the
first case fails, `combined` is skipped. Independent post-checks are still read-only
and incur no model calls. A disk/database infrastructure error can leave partial
artifacts; the runner reports failure rather than assuming preservation or success.
Token totals include only API usage actually returned, not unreported usage/cost.
Investigation time and total case/run time are reported separately. This validates
the simulated backend path, not a browser or UI.

Inspect outputs using the printed run directory (replace `<run-id>`):

```powershell
Get-Content .traces/milestone4/<run-id>/summary.json
.\.venv\Scripts\python.exe -m learnflow.agent_cli trace --file .traces/milestone4/<run-id>/stale-view/agent.jsonl
.\.venv\Scripts\python.exe -m learnflow.agent_cli trace --file .traces/milestone4/<run-id>/combined/agent.jsonl
.\.venv\Scripts\python.exe -m learnflow.tools verify_end_to_end --db .traces/milestone4/<run-id>/combined/learning.sqlite3 --student 1 --course 1
```

Exit code 0 means all checks passed; 1 means a failed check/configuration/execution;
2 means invalid CLI arguments or missing paid opt-in. Output remains under ignored
`.traces/` by default. Custom output roots should likewise stay out of commits.
The runner has been tested with offline scripted models only; it has not been
executed against a paid live model during this implementation.

### Iteration after the first combined live failure

The retained run `.traces/milestone4/6c5ee8b1c5694fd089d7cfd541a26913/` passed
`stale-view` and failed `combined`. It remains unchanged as development
evaluation-and-iteration evidence. It is not held-out evaluation.

Confirmed from the saved combined trace/snapshots: the model was configured as
`gpt-5.4-mini`; limits were 10 turns, 20 tool calls, and 2 repairs. Three model
turns occurred, with only `inspect` and `verify_end_to_end` executed. No repair
requests, tool errors, API errors, or budget exhaustion were recorded. Both checks
failed; before/after snapshots were identical. The host rejected an unsupported
recovery claim rather than accepting success.

The validator's exact recorded diagnostic is reached only for a parsed
`recovered` proposal that fails its success checks. This supports premature success
finalization under the inspected implementation. Raw model output and its original
diagnosis were **not retained**, so the original wording and raw third response
cannot be audited directly. Standard function-call extraction/dispatch is covered
by mocked SDK tests; no dropped calls are evidenced, but the original raw response
is unavailable. Confusion about verification references is a hypothesis, not a
proven account of the model's reasoning.

Verification descriptions and `comparison_references` now explicitly state:

- Progress `actual` is stored derived progress; `expected` is earned state from
  authoritative activity/quiz evidence evaluated with course rules.
- Learner-view `actual` is the cache; `expected` is **stored progress**, which may
  itself be wrong. Agreement at that layer alone is not overall correctness.

For learner-view investigations, an early unsupported `recovered` proposal with
observed mismatches may receive **one** host validation feedback message if turn,
tool-call and repair budgets remain. The same investigation continues; no new
investigation is started and no limits are extended. The host does not choose or
execute any repair. The model can continue using justified tools or honestly
escalate. Repeated unsupported success is still rejected. Explicit escalation,
unknown evidence, failed repairs, and exhausted budgets do not trigger corrective
repair pressure. API failures still terminate without retry.

Future traces retain validated, redacted `model_report_proposal` entries before
host validation and `report_validation_feedback` when used. These are structured
final-report proposals, not raw SDK output or hidden chain-of-thought. This does
not retroactively recover the missing response from the failed run.

To rerun **only combined** yourself in a fresh isolated database, without rerunning
stale-view or overwriting the failed evidence:

```powershell
$env:LEARNFLOW_MAX_REPAIRS = "2"
.\.venv\Scripts\python.exe -m learnflow.development_smoke --allow-paid --case combined
```

This remains a potentially paid development smoke test. The runner prints the new
run directory and independently checks preservation and end-to-end consistency.
No paid rerun was performed while implementing this iteration.

### Second combined failure: a rejected refresh and masked failure citation

Run `.traces/milestone4/7762f6d2f5914282a1ad5ff3f6b202f2/` is preserved unchanged
as development evaluation-and-iteration evidence. The recorded sequence was
`inspect`, `verify_end_to_end`, `recover`, `verify_end_to_end`, `refresh_view`.
Every request had validated `{}` arguments, bound to student 1/course 1. Limits
were 10 turns, 20 tool calls, and 2 repairs.

`recover` succeeded. The second verification had consistent progress and a stale
cache. The `refresh_view` request (`tool-0005`) returned
`{"evidence_ref":"tool-0005","ok":false,"error_code":"cache_refresh_not_justified"}`.
No `inspect_view` had been called. The host guard requires that explicit inspection
in addition to correct progress and observed stale cache, so it rejected the
request **before dispatching the database refresh**. Only one repair had been
dispatched; this was not repair-budget exhaustion or a demonstrated database error.
Why the model omitted inspection cannot be inferred from hidden reasoning.

The recorded report proposal honestly escalated and cited `tool-0001` through
`tool-0005`, describing the rejected refresh. `tool-0005` was a real failure result,
but the reference validator recognized only successful results as citable evidence.
It therefore replaced that useful explanation with a generic unsupported-reference
message. No fabricated reference was present in the recorded proposal.

An offline replay of the same five calls and `{}` arguments reproduced both
problems before the fix. Inspection requirements, arguments, scope, budgets, course
rules and success checks remain unchanged. Guard rejections now include structured
`missing_preconditions`, e.g. `["inspect_view"]`. The model may choose to inspect
and request refresh again within its remaining budgets, or honestly escalate; no
tool is selected automatically and rejected requests do not consume repair attempts.

The agent now separately tracks all observed result IDs and successful evidence.
Failures can be cited as failures; only successful inspections/changed repairs and
passing checks establish success. Unknown IDs still fail validation. Final actions
retain host error codes and missing prerequisites, so a report-validation problem
cannot erase the underlying operational error. The model's diagnosis remains
model-generated prose; host-recorded facts and verification remain authoritative.

Regression tests cover honest failure citation, truly unknown references,
failed verification not proving success, the same rejected sequence, and a
model-selected inspection/retry path using exactly two dispatched mutations.
Both failed runs remain preserved. No paid investigation was run for this fix;
whether the live model will use the new prerequisite detail remains unverified.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use temporary SQLite databases and cover successful course completion,
failure and retry at the passing boundary, repeated processing, out-of-order
prerequisites, unprocessed evidence, student isolation, validation, and reset.
Milestone 2 tests assert explicit progress values and mismatches for all scenarios,
recovery, source preservation, student/course isolation, no-op audits, rollback,
missing progress rows, prerequisite enforcement, and repeatable scenario setup.
Milestone 3 adds scripted-model orchestration tests, mocked official SDK HTTP
transport tests, and configuration/CLI tests. They cover repair/verification,
healthy failed quizzes, invalid/out-of-scope arguments, unknown tools, budgets,
operational failures, unsupported success, evidence preservation and isolation.
Routine tests require no credentials and never call a live model. No paid pytest
test is auto-discovered; use the explicit `live-smoke --allow-paid` CLI when ready.
Milestone 4 adds cache initialization/publication, dual-layer verification, wrong
repair ordering, distinct mocked-agent choices, repeatability, scope isolation,
source preservation, and cache failure rollback coverage.
The development runner also has offline tests for isolation, expected choices,
preservation failures, API failures, stop/no-retry behavior, usage summaries,
configuration preflight, explicit opt-in and CLI output.
Milestone 4 validation: **129 local tests passed**, retaining the existing 63
tests (SDK tool-list and CLI mock contracts extended for learner scope), including all 20 existing
milestone 1/2 tests. The installed Starlette/httpx combination emits one existing
test-client deprecation warning. This is not a live-model evaluation result.

## Remaining limitations

This is a local simulator without authentication, enrollment, pagination,
durable background jobs, schema migrations, ingestion deduplication, or production
deployment hardening. Quiz scores are synthetic inputs, not a quiz grading
service. SQLite and full student projection recalculation suit this small demo.
The simulator trusts callers to submit source evidence; future recovery-agent
access must be narrower than simulator ingestion access. See AGENTS.md for
mandatory recovery boundaries. The three initial milestone 3 live development
checks and the subsequent milestone 4 combined rerun were reported successful.
No hidden evaluation dataset is included. The cache has no TTL,
worker or automatic read-through repair. Backend consistency
does not prove that an actual learner complaint was resolved.
Verification and processing share
the deterministic rule implementation; tests supply independent explicit expected
outcomes. Logs are local operational records, not tamper-proof external audits.

## Milestone 5: local student dashboard and recovery console

Use Node.js 20.19+ (or 22.12+) and the Python environment above. Run the following
from the repository root in **PowerShell terminal 1**:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Create only if absent; edit locally, never paste a key into a command or frontend file.
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
# Set OPENAI_API_KEY and OPENAI_MODEL in that local file for paid investigations.
$env:LEARNFLOW_DEMO_MODE = "1"
$env:LEARNFLOW_MAX_REPAIRS = "2"
.\.venv\Scripts\python.exe -m learnflow.serve_demo
```

No credentials are needed to browse/reset the demo. Configuration is loaded only
after an explicitly approved investigation request; keep API keys entirely on
the server. Never use a `VITE_` variable for a secret. Existing environment values
override `.env`. The existing configurable model, timeouts, and tool/turn budgets
still apply. Two repair attempts support progress repair plus cache refresh.

In **PowerShell terminal 2**, from the repository root:

```powershell
Set-Location frontend
npm.cmd ci --offline=false
npm.cmd run dev
```

Open **http://127.0.0.1:5173**. Vite proxies `/api` to the loopback FastAPI server
on port 8000. The backend launcher deliberately uses one worker, no reload, and
binds only `127.0.0.1`; the frontend also binds loopback. Stop both with Ctrl+C.
If your machine requires an organizational TLS certificate, configure trusted
public CA certificates for Node; do not disable certificate verification.

The dashboard reads `/students/{student}/courses/{course}/learner-view`, including
its persisted version and publication time. Course percentage and lesson status
come from that cache, even when stale. Activity evidence and quiz requirements
are shown separately from earned completion. Loading, read errors and unpublished
views are explicit; reloading does not publish or repair a view.

The clearly labelled demo panel supports `missed-processing`, `partial-update`,
`healthy-control`, `stale-view`, and `combined`. It resets only the dedicated
`.local-demo/learning.sqlite3`; it ignores `LEARNFLOW_DB` and leaves previous
smoke databases and `.traces/` intact. The directory has a purpose marker and an
unmarked existing directory is rejected. Initial startup seeds this database
only if absent; later starts preserve it. Existing scenario behavior is reused:
missed/partial progress faults need progress recovery, stale-view needs cache
refresh, combined needs both, and healthy-control needs no mutation. Labels and
expected outcomes never become agent inputs.

For a demonstration, load a scenario, open **Recovery console**, review the
student/course and symptom, check the paid-consent box, then click
**Investigate with AI**. This starts one real, paid investigation. Nothing starts
on page load or scenario selection. The UI disables duplicate submissions and
resets while active; the backend enforces a single active run and idempotent
request IDs. A failed paid POST is never automatically repeated.

The incident timeline polls actual redacted trace events: tool requests/results,
timings, model usage when supplied, and the validated final report. Expand events
and snapshots for details. Before/after comparisons distinguish authoritative
evidence versus derived progress from stored progress versus learner view.
The backend independently verifies afterward. Completion triggers a read-only
view refetch, never an automatic cache repair. No browser/UI verification is
claimed by the agent.

Useful API adapters (run/reset endpoints require demo mode; catalogue and config are read-only):

| Operation | Endpoint |
| --- | --- |
| List synthetic courses | `GET /courses` |
| Read demo availability | `GET /web/config` |
| Reset dedicated demo | `POST /web/demo/scenario` with `{"scenario":"stale-view"}` |
| Start an approved investigation | `POST /web/runs` with student/course, symptom, `allow_paid:true`, UUID `request_id` |
| Reconnect to an active run | `GET /web/runs/active` |
| Poll actual events and report | `GET /web/runs/{id}` |

Per-run files are kept in `.local-demo/runs/{id}/`: `before.json`, `agent.jsonl`,
`after.json`, and `run.json`. They and the demo database are Git-ignored.
Completed runs remain available by ID while the server process lives. Restarting
the process clears the polling registry and idempotency history, not the saved
artifacts; there is no durable job queue or automatic paid-run resumption.
Do not restart the server during an investigation. This local simulator has no
authentication and must not be exposed to a network or run with multiple workers.

### Offline validation

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Set-Location frontend
npm.cmd run build
npm.cmd run test:browser
```

Browser tests start their own Vite server on port 5173, so stop the interactive
frontend first. On Windows they use installed Microsoft Edge; on other systems
install Playwright Chromium with `npx.cmd playwright install chromium` (use `npx`
outside Windows). Tests intercept all API requests with explicitly labelled
mocked responses; they never start a paid investigation. API integration tests
use the real deterministic domain/orchestration with scripted model responses.
They cover paid opt-in, scope/schema validation, local boundaries, active reset
and submission locks, request idempotency, combined recovery, trace snapshots,
source preservation, unrelated students, safe errors and dedicated database isolation.

Milestone 5 validation: **135 Python tests passed**, including all 129 existing
tests; the production frontend build and **4 mocked browser tests passed**.
Browser checks cover stale cached display, explicit consent and polling,
before/after state, post-run read-only refetch, uninitialized/error states, and
mobile layout. These results are offline checks, not live AI results. No paid API
call was made for this implementation; the new web-to-live-model path remains
unverified. The earlier live development results do not establish frontend behavior.

## Milestone 6: reproducible evaluation

The developer-only harness compares replay-only, deterministic rule-based, and
the unchanged real AI agent through the same scoped host/tool gates. It includes
12 development cases, a frozen 24-case held-out manifest, a separate 3-case
robustness manifest, an independent outcome oracle, provenance, JSON/CSV metrics,
and safe resume without repeating completed or interrupted paid attempts.

Run only the offline development baselines:

```powershell
$evaluationRun = ".traces/evaluation/development-$([guid]::NewGuid().ToString('N'))"
.\.venv\Scripts\python.exe -m learnflow.evaluation --split development --methods replay rule --max-cases 12 --output $evaluationRun
Get-Content "$evaluationRun/summary.md"
```

Development results: rule-based repair 8/8, replay-only 2/8; both healthy no-action
4/4 and source/unrelated preservation 12/12. No paid or held-out evaluation was
executed. See [docs/evaluation.md](docs/evaluation.md) for metric denominators,
limitations, frozen splits, interruption handling, and explicitly opt-in future
live commands. Mocked AI adapter tests are not live AI evaluation results.

For submission, use [docs/submission.md](docs/submission.md): it gives the exact
Combined demo steps (choosing the menu item is not loading it), confirmed scope
and evidence checks, and the frozen 24-case commands for all three methods.
Submission checks: 166 offline Python tests, production frontend build, and
6 mocked browser tests passed. No held-out or paid evaluation was executed.
