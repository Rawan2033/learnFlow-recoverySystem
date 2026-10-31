# LearnFlow Recovery — milestones 1–3

A local, synthetic learning platform for an AI hackathon. This milestone provides
the backend evidence-to-progress pipeline, reproducible developer scenarios,
diagnostics, verification, controlled recovery, and an opt-in OpenAI investigation
agent. All students are synthetic. There is no frontend. Only opted-in agent
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
   SQLite write transaction. Only processed evidence participates.
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
`read_processing_history`, `inspect`, `verify`, and `recover`. Methods accept only
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
| `LEARNFLOW_MAX_REPAIRS` | 1 | 0–5 dispatched recovery attempts |
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
scenario-to-action mappings. Only `inspect`, `verify`, and `recover` are exposed.
Every function takes exactly `{}`, using a strict schema validated locally.
Student/course/database scope is bound by the host outside model control. No
tool exposes source writes, SQL, shell, source browsing, scenarios, test answers,
credentials, repository uploads, or database uploads. All tool calls execute
sequentially, even if a model returns a batch despite parallel calls being disabled.
Symptoms and strings in diagnostic/log results are untrusted data.

Recovery is dispatched only after inspection and mismatching verification.
Invalid arguments, unknown tools and tool failures return structured errors
within the bounded loop. API errors or incomplete responses stop safely; exhausted
budgets escalate without inferring success. Recovery continues to use milestone 2
logic and its existing audit trail, preserving source rows and unrelated scope.

The final report contains status (`recovered`, `no_fault_found`, `escalated`, or
`execution_failed`), evidence references, concise diagnosis, host-recorded actions
and audit IDs, verification result, remaining limitations, and recommended next
steps. Evidence references identify actual trace tool results (e.g. `tool-0001`).
`recovered` requires a successful changed recovery and subsequent passing verification
with inspection/repair/verification references. `no_fault_found` requires inspection,
passing verification and no dispatched repair. Unsupported success claims are
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
Implementation validation: **63 local tests passed**, including all 20 existing
milestone 1/2 tests. The installed Starlette/httpx combination emits one existing
test-client deprecation warning. This is not a live-model evaluation result.

## Remaining limitations

This is a local simulator without authentication, enrollment, pagination,
background workers, schema migrations, ingestion deduplication, or production
deployment hardening. Quiz scores are synthetic inputs, not a quiz grading
service. SQLite and full student projection recalculation suit this small demo.
The simulator trusts callers to submit source evidence; future recovery-agent
access must be narrower than simulator ingestion access. See AGENTS.md for
mandatory recovery boundaries. Live model behavior remains unverified until the
opt-in smoke check is run. No hidden evaluation datasets or frontend is included.
Verification and processing share
the deterministic rule implementation; tests supply independent explicit expected
outcomes. Logs are local operational records, not tamper-proof external audits.
