# LearnFlow Recovery — milestones 1 and 2

A local, synthetic learning platform for an AI hackathon. This milestone provides
the backend evidence-to-progress pipeline, reproducible developer scenarios,
diagnostics, verification, and controlled recovery. No AI agent, frontend, real student
data, or runtime external services are included.

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
that tool allowlist and filesystem/database permissions in its host.

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

## Remaining limitations

This is a local simulator without authentication, enrollment, pagination,
background workers, schema migrations, ingestion deduplication, or production
deployment hardening. Quiz scores are synthetic inputs, not a quiz grading
service. SQLite and full student projection recalculation suit this small demo.
The simulator trusts callers to submit source evidence; future recovery-agent
access must be narrower than simulator ingestion access. See AGENTS.md for
mandatory recovery boundaries. No LLM integration, hidden evaluation datasets,
agent orchestration, or frontend is included. Verification and processing share
the deterministic rule implementation; tests supply independent explicit expected
outcomes. Logs are local operational records, not tamper-proof external audits.
