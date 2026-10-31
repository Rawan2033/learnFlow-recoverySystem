# LearnFlow Recovery

Build a simulated learning platform for an AI hackathon. The eventual recovery
agent will diagnose and repair incorrect derived learning progress.

Current scope includes milestones 1 and 2: Python, FastAPI, SQLite, and pytest; one synthetic
course, three sequential lessons, synthetic students, stored source evidence,
explicit deterministic processing, diagnostics, verification, and controlled recovery. Do not
implement an AI agent, frontend, external services, or real student data.

The future recovery agent must never:

- Modify source quiz results (including scores or their student/lesson identity).
- Invent, fabricate, or insert completion evidence to justify a repair.
- Access hidden fault labels, test answers, or evaluation ground truth.

Keep activity events and quiz results separate from derived progress. Repairs
must be justified by existing source evidence and deterministic course rules.
Preserve idempotent processing and transactional consistency. Source ingestion
and synthetic seed/reset are simulator operations, not recovery permissions.
Any future agent interface must enforce these boundaries in code; this document
alone is not an authorization mechanism.

`learnflow/scenarios.py` is developer-only fault injection. Never expose it,
the seed/reset command, arbitrary SQL, source ingestion endpoints, or test files
as recovery-agent tools. Scenario labels and test expectations must stay out of
tool responses and operation history. `RecoveryTools` provides the future tool
surface; register only its explicit diagnostic, verify, and recover methods.
Bind its database path in trusted host code, not in agent-provided arguments.
Recovery may write derived progress, replay receipts, and operation audits only;
it must preserve all source event and quiz-result rows, including timestamps.

Run `python -m pytest -q` after backend changes. Tests use isolated temporary
databases. Never point a test or seed/reset command at a database to preserve.
