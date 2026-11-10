# mini6 + Dify Testing Agent (ARC-Bench adapter)

This is the existing mini-SWE-agent Coding Agent **plus an optional Dify post-generation testing stage**. It contains no fixed 12306/Ctrip/Keep/BookStack test cases. The Dify Workflow dynamically derives tests from the current `requirements.yaml` and the generated application's UI/route hints. The local Playwright runner checks them against the generated app and sends failure evidence to the Dify `analyze` branch.

## Files changed

- `main.py` — runs the optional stage **after** successful mini-SWE-agent submission; reserves time if and only if the Dify key is available.
- `arc_adapter.py` — optional `extra_task` parameter for a **new, bounded** repair Coding Agent run.
- `arc_dify_pipeline.py` — orchestration, application startup/cleanup, generated test execution, reporting and optional repair.
- `mini6_dify_batch.py` — splits current YAML into requirement/scenario units (unchanged business-agnostic generator).
- `mini6_dify_client_streaming.py` — Dify API SSE transport.
- `mini6_dify_run_tests.py` — Playwright execution and optional Dify failure analysis.
- `scripts/package_agent.py` — allowlists the new modules, excludes credentials and local outputs.

## Local setup

Your existing `.env` can continue to contain the normal model variables and:

```ini
DIFY_API_BASE_URL=https://api.dify.ai/v1
DIFY_API_KEY=<your-private-Dify-WORKFLOW-API-key>
ARC_DIFY_MODE=auto
ARC_DIFY_LIMIT=1
ARC_DIFY_REPAIR_ROUNDS=0
ARC_DIFY_RESERVE_SECONDS=360
```

The sample above is documentation **only**. Do not add real keys to any file being submitted. Use the key for your *published Dify workflow*, not the DeepSeek provider key. The published Dify workflow must have `generate` and `analyze` modes and return the same `result` JSON used by your successful local experiments.

Test with:

```bash
python main.py input --output-dir outputs/demo
```

A successful Coding Agent run creates the app. If Dify is available, the integration then dynamically generates up to `ARC_DIFY_LIMIT` scenario tests, launches a newly built app on a free localhost port, runs Playwright, saves the result, and optionally analyzes errors. Stage artifacts go under `outputs/demo/.arc_adapter/dify_testing/`. The app remains submitted even if the optional tests fail.

For local fast smoke testing, use `ARC_DIFY_LIMIT=1`; after validating the pipeline, raise the limit (`3`, `9`, etc.) and provide sufficient time. Use `ARC_DIFY_REPAIR_ROUNDS=1` to enable one bounded Coding Agent repair and rerun the **same frozen generated tests**. Default is `0` to avoid extra model usage or unverified changes in the first benchmark run.

Generated TypeScript is arbitrary executable code. The test directory and scrubbed environment are **not a security sandbox**. Only run Dify-produced tests in a disposable/containerized environment with no unrelated files, production data, or secrets. Keep external traffic and competition-data handling within your platform's rules. The initial pipeline is a research prototype, not a security boundary.

## ARC-Bench deployment

```bash
python scripts/package_agent.py
```

Upload `dist/mini_swe_arcbench_adapter.zip` as the Python Agent. **Do not upload your project folder or `.env`.** The package excludes `.env`, `.env.example`, local `input/`, `outputs/`, `node_modules/`, and generated test files. The normal `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL` values are expected to be injected by ARC-Bench as in the original mini6.

**ARC-Bench does not automatically get `DIFY_API_KEY` from your computer.** Without a supported, secure custom-secret injection mechanism, the integration logs:

```
[DIFY-INTEGRATION] status=skipped_no_dify_api_key_original_agent_preserved
```

and submits the original Coding Agent output. This is **not** evidence that Dify was used in the ARC-Bench run. If the platform requires Dify, `ARC_DIFY_MODE=required` changes missing-key status to a CLI failure. Never embed your key in Python files, requirements YAML, ZIP, a public URL, or stdout. Ask the platform operator whether third-party Dify Cloud calls and secure secret injection are supported. The earlier HTTP 405 probe proves HTTPS connectivity only, not policy permission or authenticated Workflow execution.

If secure key injection is unsupported, keep `ARC_DIFY_MODE=auto` for a safe baseline submission, and use the Dify integration locally. An alternative platform-only Testing Agent could be implemented later using `OPENAI_*` injection, but **this package does not silently switch to that separate architecture**.

## Note on evaluation

Dify-generated tests are **internal feedback**, not the official ARC-Bench benchmark. A green local test suite does not imply the official validation suite will pass, and a generated-test failure can stem from a mistaken assumption in the test rather than the application. Inspect original requirements, test fixtures, missing data, and the local reports. Do not modify official tests or weaken an actual requirement to chase a score.
