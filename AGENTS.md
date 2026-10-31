# LearnFlow Recovery

Build a simulated learning platform for an AI hackathon. The eventual recovery
agent will diagnose and repair incorrect derived learning progress.

Current scope is milestone 1: Python, FastAPI, SQLite, and pytest; one synthetic
course, three sequential lessons, synthetic students, stored source evidence,
explicit deterministic processing, and inspectable derived progress. Do not
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

Run `python -m pytest -q` after backend changes. Tests use isolated temporary
databases. Never point a test or seed/reset command at a database to preserve.
