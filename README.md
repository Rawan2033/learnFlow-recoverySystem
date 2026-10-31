# LearnFlow Recovery — milestone 1

A local, synthetic learning platform for an AI hackathon. This milestone provides
the backend evidence-to-progress pipeline. No AI agent, frontend, real student
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

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use temporary SQLite databases and cover successful course completion,
failure and retry at the passing boundary, repeated processing, out-of-order
prerequisites, unprocessed evidence, student isolation, validation, and reset.

## Remaining limitations

This is a local simulator without authentication, enrollment, pagination,
background workers, schema migrations, ingestion deduplication, or production
deployment hardening. Quiz scores are synthetic inputs, not a quiz grading
service. SQLite and full student projection recalculation suit this small demo.
The simulator trusts callers to submit source evidence; future recovery-agent
access must be narrower than simulator ingestion access. See AGENTS.md for
mandatory recovery boundaries. Fault injection, hidden evaluation datasets,
diagnosis, and repairs are future milestones.
