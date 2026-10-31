# Submission demo and final evaluation

## Load the intended Combined incident first

Read-only inspection of the three saved local demo runs found the correct scope
(student 1, course 1) but zero activity events and zero quiz results in each
inspection. The current `.local-demo/learning.sqlite3` also had no evidence.
Thus those runs did not investigate the Combined fixture. The saved artifacts
cannot establish the exact browser click history or whether an earlier server
used another working directory.

The scenario menu only chooses what to load; **Load scenario** actually applies
it. Previously changing the choice left the old report visible. The submission
fix explicitly distinguishes selection from application, clears old reports and
paid consent on a choice change/load, consumes the backend's returned scope IDs,
and confirms actual evidence counts and both initial verification results.
Starting a new run also clears the previous report while the POST is pending.
No agent instructions, recovery rules, eligibility rules or evaluation cases
were changed to force a result.

After any active run finishes, stop the old backend with Ctrl+C and restart it
from the repository root so both UI and API use the updated implementation:

```powershell
Set-Location C:\Users\acer\Desktop\learnflow-recovery
$env:LEARNFLOW_DEMO_MODE = "1"
$env:LEARNFLOW_MAX_REPAIRS = "2"
.\.venv\Scripts\python.exe -m learnflow.serve_demo
```

In a second terminal, start the frontend if it is not already running:

```powershell
Set-Location C:\Users\acer\Desktop\learnflow-recovery\frontend
npm.cmd run dev
```

1. Open `http://127.0.0.1:5173` and hard-refresh (Ctrl+F5).
2. Select **Synthetic Alex** and **Learning Foundations**. Their IDs are 1 and 1.
3. In **LOCAL DEMO CONTROLS**, choose **Combined faults**. This alone does not
   change the database or start an investigation.
4. Click **Load scenario**. Wait for the confirmation: **Loaded in this tab:
   Combined faults**, **Student 1, course 1**, **2 events, 1 quiz result**, and
   **Progress mismatch; learner view mismatch**. The menu load resets only the
   dedicated demo database; previous run artifacts are retained.
5. The dashboard reads the actual cached view: lesson 1 appears completed, lesson
   2 locked, lesson 3 locked (33%). This is intentionally stale: stored progress
   incorrectly has lesson 1 incomplete, while its activity and passing score 90
   exist. Do not infer earned correctness from the cached card alone.
6. Open **Recovery console**. The old report should be absent. Keep the symptom
   `Please investigate why my next lesson is locked.` Labels are not agent inputs.
7. **Stop here for a no-cost demo setup.** To deliberately run a paid demonstration
   later, check the one-run consent box and click **Investigate with AI** once.
   Expected model-selected operations are progress recovery, verification, then
   justified view refresh and verification; do not force that sequence or promise
   live success. After successful recovery, lesson 2 should be available, not
   completed. Backend verification is not browser/UI verification.

The Vite proxy points to `127.0.0.1:8000`; `serve_demo` constructs one shared
DemoService database for scenario setup, domain reads and scoped agent runs.
It ignores `LEARNFLOW_DB` in enabled demo mode. Starting a different backend
(`learnflow.app`) does not enable these demo endpoints. Do not seed a smoke-test
database and expect the browser to read it. If confirmation is missing, stop and
fix the server/setup issue before paying for another investigation.

## Freeze code and run the final held-out evaluation

These commands are instructions for the user, **not implementation validation**.
No held-out case (including baselines) or paid evaluation was run while preparing
this submission. Do not change/tune the agent after inspecting held-out outcomes.

The final implementation is committed locally before evaluation; the exact hash
is in the accompanying completion message and `.traces/submission-ready.json`.
All methods below use the same frozen 24-case manifest, one attempt per case,
fresh isolated databases, and identical existing budgets. They use the actual
supported `learnflow.evaluation` CLI. From the repository root:

```powershell
Set-Location C:\Users\acer\Desktop\learnflow-recovery
if (git status --porcelain) { throw "Commit or resolve tracked changes before evaluation." }
$submissionCommit = (git rev-parse HEAD).Trim()
$expectedHash = (Get-Content evaluation/manifests/heldout.sha256 -Raw).Trim()
$actualHash = (Get-FileHash evaluation/manifests/heldout.json -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) { throw "Frozen held-out manifest hash mismatch." }
if ((Get-Content evaluation/manifests/heldout.json -Raw | ConvertFrom-Json).cases.Count -ne 24) { throw "Expected 24 held-out cases." }
$evalRoot = ".traces/evaluation/submission-$([guid]::NewGuid().ToString('N'))"
$limits = @('--max-turns','10','--max-tool-calls','20','--max-repairs','2','--timeout','30','--max-output-tokens','4096')
Write-Host "Submission commit: $submissionCommit"
Write-Host "Frozen manifest: $expectedHash"
Write-Host "Evaluation root: $evalRoot"
```

Replay-only (no paid calls):

```powershell
.\.venv\Scripts\python.exe -m learnflow.evaluation --split heldout --unlock-heldout --methods replay --max-cases 24 --repetitions 1 @limits --output "$evalRoot/replay"
if ($LASTEXITCODE -ne 0) { throw "Replay run stopped; preserve its results and inspect the failure." }
```

Rule-based (no paid calls):

```powershell
.\.venv\Scripts\python.exe -m learnflow.evaluation --split heldout --unlock-heldout --methods rule --max-cases 24 --repetitions 1 @limits --output "$evalRoot/rule"
if ($LASTEXITCODE -ne 0) { throw "Rule run stopped; preserve its results and inspect the failure." }
```

Real AI (**paid**; ensure `OPENAI_API_KEY` and `OPENAI_MODEL` are configured in the
existing local `.env`, without printing them):

```powershell
.\.venv\Scripts\python.exe -m learnflow.evaluation --split heldout --unlock-heldout --methods ai --allow-paid --max-cases 24 --repetitions 1 @limits --output "$evalRoot/ai"
if ($LASTEXITCODE -ne 0) { throw "AI run stopped; preserve its completed/failed attempts. No automatic retry." }
```

That authorizes at most 24 investigations, each with up to 10 model turns, not
just 24 individual API requests. SDK retries are disabled. API failures stop the
invocation; failed attempts are retained. Do not loop/re-run a failed case until
success. If explicitly resuming pending cases, repeat that method's exact command
with the same path/settings and add `--resume`; a started/failed/interrupted case
will not be repeated. Do not generate a new path to erase a failure.

Inspect saved results without additional model calls:

```powershell
Get-Content "$evalRoot/replay/summary.md"
Get-Content "$evalRoot/rule/summary.md"
Get-Content "$evalRoot/ai/summary.md"
Import-Csv "$evalRoot/ai/results.csv" | Format-Table case_id,status,correct,repair_attempts,tool_calls,total_tokens
```

Each method folder contains its own `run.json` with commit, clean/dirty status,
model, manifest and prompt/schema hashes, budgets and settings. Per-attempt
starting-state hashes support checking fair comparisons. Results and databases
remain ignored under `.traces/`. Read [evaluation.md](evaluation.md) for metric
denominators, known-family generalization limits, and interpretation of partial runs.

## Offline submission checks

166 Python tests passed, including the new backend API scenario-loading
integration check against a temporary database; the production frontend build passed; all
6 browser tests passed with explicitly mocked API responses. The user's running
port 5173 was left alone; browser checks used an isolated server on port 5199:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Set-Location frontend
npm.cmd run build
$env:LEARNFLOW_BROWSER_TEST_PORT = "5199"
npm.cmd run test:browser
```

No paid call, held-out execution, existing database reset, push or agent tuning
was performed as part of these checks.
