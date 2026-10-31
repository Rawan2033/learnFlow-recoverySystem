# LearnFlow Recovery evaluation (milestone 6)

For the final three-method held-out commands and browser demo setup, see
[submission.md](submission.md). Those commands are provided for deliberate user
execution; preparing them does not execute the held-out evaluation.

This is an outcome-based evaluation of the existing backend agent and tools.
It does not evaluate a browser, teaching quality, or whether an external learner
complaint was resolved. The application, frontend, agent instructions, tool
definitions and production verification gates are unchanged by this milestone.

## Methods and shared boundary

All three methods run through the **same existing `Investigator`**, using the
same six tools, strict empty-object schemas, host-bound student/course,
preconditions, sequential mutations, learner-view success gates, and limits.
The baseline policies implement the existing model/tool protocol; they cannot
open a database or see the harness's manifest, labels, expected answers or score.
They act only on scoped diagnostic/verification results. Production verification
guides decisions; independent fixture-based scoring judges their outcomes.

- **Replay-only (`replay`)** inspects evidence, inspects the view, and verifies.
  When progress mismatches justify replay it requests `recover`, then verifies.
  It never refreshes the view, never replays healthy progress, and escalates
  remaining mismatches or tool failures. This is a deliberately limited replay
  strategy, not an unconstrained write that bypasses the normal preconditions.
- **Deterministic rule-based (`rule`)** performs those inspections and checks,
  replays only incorrect progress, re-verifies, refreshes only when progress is
  correct but the inspected view is inconsistent, then verifies again. Healthy
  states require no mutation. It stops on errors and does not retry a failed
  repair. It is intended as a competent baseline, not a straw man.
- **AI (`ai`)** adapts the existing real `OpenAIResponsesModel` and `Investigator`
  directly. The model chooses actions; no case-to-action mapping or baseline
  policy is used for real AI execution. Test injection is explicitly recorded
  as `mocked_ai`, never a live result. Baseline traces use the common host's
  `model_turn`/`model_report_proposal` event names for policy turns/proposals;
  these names do not imply a model API was called.

Default evaluation budgets for **every** method: 10 turns, 20 tool requests,
2 dispatched repair attempts, 30 seconds per model request, 4096 output tokens.
They can be set with `--max-turns`, `--max-tool-calls`, `--max-repairs`,
`--timeout`, and `--max-output-tokens`. The evaluation's recorded CLI budgets
take precedence over environment budget overrides, for fair comparisons. These
options do not tune or change the production agent. A complete rule-based
combined investigation takes eight protocol turns including its final report.

## Splits, fixtures, and freeze

`evaluation/manifests/` contains:

| Split | Cases | Composition | Execution during implementation |
| --- | ---: | --- | --- |
| development | 12 | 2 each missed-processing, partial-update, stale-view, combined; 4 healthy | Both baselines, offline |
| heldout | 24 | 4 each of the same four failures; 8 healthy | **Not executed**, including baselines |
| robustness | 3 | Persistent pre-dispatch recover, refresh, or verification outage | Separate set not run; equivalent failure behavior tested on development fixtures |

Configurations differ across splits, not just names/times. Variations include
all three supported synthetic student IDs, target lessons 1–3 (partial downstream
eligibility faults target lessons 1–2), scores below/at/above the seeded 70-point
threshold, pass/fail histories, repeated activity events, missing activity,
unlinked quiz rows, unmet prerequisites and downstream evidence. A previous pass
is not revoked by a later failed quiz, matching the platform's documented rule.
We keep the seeded single course and its three sequential lessons fixed; there
is no evaluation of alternate course structures, enrollments, or longer courses.

These measure **generalization to new configurations within known fault
families**, not unseen fault types. The developer authored both splits. Their
public manifests are privileged harness inputs, not agent context; they are not
secret adversarial benchmarks. Do not expose repository/file browsing to methods.

`heldout.sha256` freezes the exact held-out JSON bytes before live evaluation.
Git attributes preserve LF line endings across operating systems. Loading heldout
checks this hash. Every run also saves its manifest and digest. Static checks of
counts, disjoint configurations and the digest are not execution of held-out cases.
No held-out database has been constructed by the validation commands/tests.
Any later changes require a new version/split and an explanation. Once results
are inspected for tuning, that set is no longer unseen; use a new held-out set
for subsequent claims. The `--unlock-heldout` flag is an explicit execution gate,
not a guarantee that someone has followed this evaluation policy.

Each attempt gets a **fresh database per case × method × repetition**. Fixed
synthetic timestamps and IDs make logical starting-state hashes identical for
each comparison. Setup refuses to reuse a database. It never points at the web
demo, default application database, or earlier smoke databases. Fixture labels
are never written to operation history or passed into a symptom/tool response.

## Independent oracle and metrics

`learnflow/eval_support/fixtures.py` independently evaluates source rows: a linked
passing quiz, an activity event and earned preceding lessons are required.
It does not import production processing, recovery, or verification functions.
Expected progress is frozen before execution. Expected cache content is that
earned progress, including course completion and its independently computed
fingerprint, with a positive publication version. Exact version numbers or
publication timestamps are not correctness targets. Tests assert explicit flags
for failed quizzes, missing activities, missing prerequisites and pass boundaries.
The production verifier is not the scoring oracle.

The JSON summary always records planned, recorded and pending attempt counts.
Rows include operational failures, interruptions and unsuccessful escalations;
none are rerun until success or discarded. Denominators below are **recorded
attempts**, including failures/interruption checkpoints, in the applicable
method/category. Pending attempts are separately reported and are not successes.
Do not compare a partial AI run against a completed baseline run without showing
that coverage difference. Repetitions count as attempts, not independent students.

| Metric | Numerator / denominator |
| --- | --- |
| Correct final repair | Repairable attempts whose progress AND view match the independent pre-run oracle, with preserved sources/unrelated state / all recorded repairable attempts |
| Healthy no-action | Healthy attempts with correct state, zero mutation requests and accepted `no_fault_found` / recorded healthy attempts |
| Correct escalation | Controlled-unrecoverable attempts remaining inconsistent, preserved, and reporting `escalated` or `execution_failed` / recorded controlled-unrecoverable attempts |
| Incorrect unlock or unearned completion | Attempts with any overgranted eligibility/completion in either progress or view versus the independent oracle / all recorded attempts; individual layer/lesson/field violations are retained |
| Source preservation | Exact equality of all source event and quiz rows, including timestamps / all recorded attempts |
| Unrelated preservation | Exact equality of other students' progress, cache, receipts, audits, plus shared catalogue / all recorded attempts |
| False accepted success | Accepted `recovered`/`no_fault_found` without independent correctness, preservation and executed supporting proof / all recorded attempts; accepted-success count is also reported |
| False success proposals | Unsupported success proposals at the instant proposed / all success proposals, including host-rejected proposals and bounded continuations |

The proposal annotation is host-only and never fed back to the method. Successful
proof requires recorded inspection, view inspection and passing verification;
`recovered` also requires a changed repair and subsequent verification, while
`no_fault_found` requires no mutation. Expected outcomes do not depend on one
exact tool sequence, report wording, or the production verifier's assertion alone.

Reports also retain per-attempt dispatched repair attempts, all mutation requests
(including guard rejections), total tool calls, policy/model turns, wall latency
and reported input/output/total tokens. Unnecessary attempts/requests are those
above a lower bound of one write per initially incorrect layer; healthy mutations
are therefore unnecessary. This conservative count does not judge every redundant
diagnostic read. Time includes local orchestration, trace/scoring overhead and
tools, not only model latency. Token usage is null when unavailable/not applicable,
not fabricated zero. Summaries show how many attempts supplied usage. For a hard
interruption, unknown whole-attempt latency is null, excluded only from latency's
measured denominator; the attempt remains in the outcome denominators.
If a mutation was recorded as started but its result was lost, its dispatch
count is unknown (`null`), not invented as zero. Pending call IDs are retained;
repair totals report their measured-attempt denominator. Such attempts are never
replayed. Interrupted token usage is the known partial usage, not a billing total.

Controlled robustness outages persist for the whole attempt and raise a generic
tool failure **before dispatch**, without leaking injection labels. A missing
completion requirement is a healthy lock, not a deliberately unrecoverable fault.
These artificial operational outages test bounded escalation, not all SQLite,
network, crash, or transient-retry behavior. All methods share the same outage
and budget. An unrecoverable case must never be credited for claiming success.

## Exact PowerShell commands

From the repository root, install the existing Python requirements if needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Offline development run, both baselines, fresh output directory:

```powershell
$devRun = ".traces/evaluation/development-$([guid]::NewGuid().ToString('N'))"
.\.venv\Scripts\python.exe -m learnflow.evaluation --split development --methods replay rule --max-cases 12 --repetitions 1 --output $devRun
Get-Content "$devRun/summary.md"
Import-Csv "$devRun/results.csv" | Format-Table case_id,method,status,correct,repair_attempts,tool_calls
```

All artifacts are Git-ignored under `.traces/`. `run.json` records Git commit,
dirty status/paths, Python/package/SQLite versions, Python source hash,
instructions/tool/report-schema hashes, manifest hash, scope mode, model,
budgets, methods, cases, repetitions, and planned attempts. The full manifest is
saved for reproduction. Each attempt retains `started.json`, its isolated DB,
`before.json`, `after.json`, redacted `agent.jsonl` and `result.json`. Aggregate
`results.json`, flattened `results.csv`, `summary.json` and readable `summary.md`
are generated after each attempt. Labels/expected answers in these harness
artifacts are never uploaded to the model.

**Optional paid development evaluation; not run for this milestone:** configure
`OPENAI_API_KEY` and `OPENAI_MODEL` in the existing local `.env` (never read them
out or put keys in commands). Then deliberately opt into at most two case attempts:

```powershell
$aiRun = ".traces/evaluation/ai-development-$([guid]::NewGuid().ToString('N'))"
.\.venv\Scripts\python.exe -m learnflow.evaluation --split development --methods ai --allow-paid --max-cases 2 --repetitions 1 --output $aiRun
Get-Content "$aiRun/summary.md"
```

`--max-cases` selects a deterministic manifest prefix. Planned maximum paid
investigations = selected cases × repetitions (not tool calls). Both explicit
`--allow-paid` and `--max-cases` are required for AI; paid opt-in is required again
on resume. The existing SDK has no automatic HTTP retries. A model/API error or
adapter failure stops further attempts in that invocation; failed attempts stay
recorded. This does not silently retry a request or automatically raise budgets.

**Held-out live evaluation is intentionally deferred. Do not execute it as setup
or routine validation.** After freezing code/prompts/budgets and reviewing cost,
this opt-in command would evaluate a bounded prefix of the frozen set:

```powershell
# FUTURE ONLY: consumes held-out cases and paid API calls.
Get-Content evaluation/manifests/heldout.sha256
$heldRun = ".traces/evaluation/heldout-$([guid]::NewGuid().ToString('N'))"
.\.venv\Scripts\python.exe -m learnflow.evaluation --split heldout --unlock-heldout --methods ai --allow-paid --max-cases 24 --repetitions 1 --output $heldRun
```

Future offline robustness execution, separately labelled (not part of the
development/held-out benchmark):

```powershell
$robustRun = ".traces/evaluation/robustness-$([guid]::NewGuid().ToString('N'))"
.\.venv\Scripts\python.exe -m learnflow.evaluation --split robustness --methods replay rule --max-cases 3 --output $robustRun
```

## Resume and interruptions

Resume with the **same arguments, code/provenance, model, manifest and budgets**,
adding `--resume` and reusing the original path (not a new GUID):

```powershell
.\.venv\Scripts\python.exe -m learnflow.evaluation --split development --methods replay rule --max-cases 12 --repetitions 1 --output $devRun --resume
# Paid resume also requires renewed opt-in; it processes pending cases only.
.\.venv\Scripts\python.exe -m learnflow.evaluation --split development --methods ai --allow-paid --max-cases 2 --repetitions 1 --output $aiRun --resume
```

An OS lock prevents concurrent evaluation of the same run directory. Completed
attempt files are never overwritten. Ctrl+C records an interruption where possible
and stops. After hard termination, a reserved attempt without `result.json` is
marked interrupted on resume; retained trace and current state are inspected
read-only, never resumed/replayed. Any saved usage is retained. Only previously
unstarted attempts run. A paid request may have been billed before interruption;
there is no claim that it was cancelled or rolled back. No automatic paid retry
or process resumption occurs. A fresh run is an explicit new experiment, not a
way to erase failures; retain and report the old experiment too.

## Development findings and limitations

The development baselines completed 12 cases per method: rule-based correct
repair **8/8**, replay-only **2/8**; both healthy no-action **4/4**. Both preserved
sources/unrelated students **12/12**, with **0/12** incorrect unlock/completion
attempts and **0/12** false accepted successes. The replay baseline's six
remaining attempts escalated rather than claiming a cache repair. No unnecessary
repair attempts were observed. Development has no deliberately unrecoverable
cases, so escalation's applicable denominator is **0**, not a 100% claim.

These are small synthetic development results, not evidence that AI outperforms
the deterministic method. No live AI evaluation or held-out evaluation has been
run, and there are no fabricated AI scores. Mocked AI tests validate plumbing,
gates, error handling and accounting only. The reference implementation is small
and separately written but shares the stated conceptual rules and SQLite fixture
schema; this is not an independently commissioned external benchmark. There is
no browser evaluation, unseen-fault claim, statistical significance claim, course
structure sweep, or production reliability claim. Existing live development smoke
checks from earlier milestones are not evaluation results for this harness.

Validation: **165 offline Python tests passed**, retaining all 135 existing tests
and adding 30 evaluation checks. There is one existing Starlette/httpx
test-client deprecation warning. The frontend and production agent/domain code
were not changed. The saved development runs are labelled local development
experiments; no held-out or paid run was performed.
