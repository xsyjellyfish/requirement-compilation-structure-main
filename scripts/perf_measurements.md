# Performance measurements (before / after)

All numbers below were produced on this machine from the recorded demo artifacts in
`output/demo2` (4 requirements, 3 pages, 4 triggers, 4 APIs) and from `compiler.log`.
No model call was made to measure request size: the payload is built by the real
selection code and serialized exactly as the agent loop serializes it.

Reproduce:

```bash
python scripts/analyze_run_log.py output/demo/compiler.log output/demo2/compiler.log
python scripts/audit_prompt_payload.py output/demo2 --mode both --json scripts/payload_compare.json
```

## 1. Where the time and tokens went (recorded run)

`scripts/run_log_summary.txt`, from the two `compiler.log` files:

| run | stage 12 time | stage 12 calls | stage 12 input tokens | all input tokens | all output tokens |
|---|---:|---:|---:|---:|---:|
| demo | 469.0 s (31%) | 8 | 274,616 (53%) | 520,367 | 197,427 |
| demo2 | 465.0 s (31%) | 9 | 310,075 (57%) | 543,264 | 198,806 |

* stage 12 averaged **34,000 input tokens per call**, three times the run mean (11,300).
* model round-trip latency was 1,048 s of 1,514 s (69%); the remaining 466 s is compiler
  and browser work (13 `tsc`, 9 Playwright runs, 2 `npm install`).
* every requirement used 1–2 rounds in these runs, so no run in the artifacts shows the
  long repair loops of the ARC-Bench run. `--max-turns` is 10 per agent.

## 2. Request size per agent call (measured, no model call)

`scripts/payload_compare.json`. "chars" is the serialized `{task, files, feedback}` body;
"~tokens" is chars / 3.6 (estimate). The legacy column reproduces the pre-change selection
verbatim; the probe's legacy estimate per call (33–39 k) matches the 34 k tokens per call
that the API reported for the same stage, so the model is calibrated to within ~10%.

| requirement | phase | legacy files | legacy chars | legacy ~tokens | focused files | focused chars | focused ~tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| REQ-1 | test | 30 | 122,595 | 34,054 | 8 | 75,963 | 21,101 |
| REQ-1 | impl | 29 | 120,156 | 33,377 | 11 | 78,438 | 21,788 |
| REQ-2 | test | 30 | 135,289 | 37,580 | 9 | 94,299 | 26,194 |
| REQ-2 | impl | 29 | 132,929 | 36,925 | 13 | 99,377 | 27,605 |
| REQ-3 | test | 30 | 131,092 | 36,414 | 9 | 87,814 | 24,393 |
| REQ-3 | impl | 29 | 128,732 | 35,759 | 13 | 92,480 | 25,689 |
| REQ-4 | test | 30 | 129,116 | 35,866 | 9 | 85,177 | 23,660 |
| REQ-4 | impl | 29 | 126,761 | 35,211 | 13 | 88,614 | 24,615 |
| **total** | | | **1,535,248** | **426,458** | | **1,061,071** | **294,742** |

* payload per call: **−31 %** overall (test rounds −34 %, implementation rounds −29 %);
* supplied files per call: 29.5 → 10.6 on average (**−64 %**);
* source characters per call: 87–91 k → 46–58 k (the whole `frontend/src` and `backend/src`
  trees are no longer re-sent);
* the task half of the payload (requirement, page subtrees, trigger designs, API contracts)
  stays at 27–38 k chars, because it is the specification. The Stage 12 test task no longer
  repeats `db_schema.entities` (−4.2 k chars per test call): the E2E spec drives real UI
  input and never opens the database, so the entity schema is not task context for it.

## 3. Repair-loop budget

`STALL_LIMIT = 3` in `infrastructure/shared/agent/loop.py`: the same classified failure three
times in a row stops the loop instead of spending the remaining rounds. A task that would have
burned the full `--max-turns 10` now stops at round 3:

* 7 model calls avoided per stalled task;
* 7 × ~25 k ≈ 175 k input tokens avoided per stalled task (focused policy);
* 7 × (2.1 s `tsc` + 12.9 s Playwright) ≈ 105 s of local checks avoided per stalled task.

A changed failure keeps the whole budget (`tests/test_agent_budget.py`), so this only removes
repetition, never a genuine attempt.

On top of the stall rule, each repair task carries a hard ceiling
(`repair_budget()` in the same module): `ARC_REPAIR_BUDGET_SECONDS` (default 1200) and
`ARC_REPAIR_BUDGET_TOKENS` (default 600,000) measured from the moment the task starts, with `0`
disabling either limit. Exceeding it raises `AgentBudgetExceeded`, which the recovery step
records in the stage report (`"budget": {"seconds": …, "tokens": …}`) instead of letting a
task spend the rest of the run. The check reads `model_tokens_used()` from the task start, so a
ceiling of 600 k tokens is ~17 calls at the legacy 34 k input tokens per call, or ~24 calls under
the focused selection (Table 2).

## 4. Local check time (measured on `output/demo2`)

| operation | measured |
|---|---|
| `tsc --noEmit` over `src/**/*.ts(x)` + one spec | 2.09 s |
| Playwright E2E, one spec, real backend + isolated DB | 12.94 s |
| `npm install` (warm cache, tree up to date) | 2.56 s |
| `install_dependencies` reuse (marker matches) | 0.32 s |

A round whose static API contract already failed now returns before `tsc` and the browser run
(≈ 15 s per such round), and step 12 no longer repeats the `npm install` step 10 performed.

## 5. Test status

`python -m unittest discover -s tests -q`:

| | tests | failures | skipped |
|---|---:|---:|---:|
| before | 292 | 2 (pre-existing `test_console_logging` httpx timeout assertions) | 0 |
| after | 394 | 0 (the timeout assertion now holds: the transport gets a plain `600`, so the SDK's own timeout is not nested) | 1 |

New coverage added with these changes:

* `tests/test_agent_budget.py`: failure classification, signature stability, stall budget,
  archived category, focused selection, progressive disclosure, page-path fallback,
  dependency reuse, and the per-task ceiling;
* `tests/test_console_logging.py`: per-call ledger and per-task rows that partition the stage
  total exactly (`sum(task seconds) == usage["seconds"]`);
* `tests/test_http_tests.py`: a round that only rewrote its own suite does not re-run accepted
  suites, while a round that also changed a shared file does;
* `tests/test_file_agent.py`: a gate can tell this round's edits (`round_changed`) from
  everything changed so far;
* `tests/test_compiler.py`: CLI tests stop writing a report through a mocked compiler path.

## 6. Model usage accounting

`infrastructure/utils/presentation/model_io.py` now keeps three views of the same numbers:

* per call — the `response` event of the response log carries `usage` (prompt/completion/total
  tokens, seconds, activity) next to the raw reply, and `show_call_totals()` prints a per-task
  summary: calls, tokens, seconds, share of the stage;
* per stage — `logging.model_usage()` totals what the stage spent, printed at stage end and
  persisted as `report["model_usage"]` so it survives the terminal;
* per task — `model_stats["tasks"]` keys the same totals by activity, which is what the budget
  ceiling reads (`model_tokens_used()` since the task start).

Task seconds are summed unrounded so the task rows add up to the stage total exactly; only the
displayed values are rounded.
