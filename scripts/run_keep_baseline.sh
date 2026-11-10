#!/usr/bin/env bash
# Run from anywhere: bash scripts/run_keep_baseline.sh /path/to/arc-bench /path/to/output/keep
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$(cd -- "${1:?Pass a local clone of https://github.com/code-philia/arc-bench}" && pwd)"
OUTPUT="${2:?Pass a NEW, empty output directory}"
for TASK in "$BENCH/keep" "$BENCH/arc-bench/keep" "$BENCH/webapp/keep"; do
  [[ -f "$TASK/requirements/requirements.yaml" ]] && break
done
INPUT="$TASK/requirements"
TESTS="$TASK/tests"
[[ -f "$INPUT/requirements.yaml" && -d "$TESTS" ]] || { echo "Invalid ARC-Bench repo layout: $BENCH" >&2; exit 2; }
if [[ -d "$OUTPUT" && -n "$(ls -A "$OUTPUT")" ]]; then
  echo "Baseline requires a new empty output directory: $OUTPUT" >&2; exit 2
fi
mkdir -p "$OUTPUT"
OUTPUT="$(cd -- "$OUTPUT" && pwd)"
python "$HERE/main.py" "$INPUT" --output-dir "$OUTPUT"

# Do not modify the official tests. Use a separate test folder.
TESTDIR="$(mktemp -d "${TMPDIR:-/tmp}/arc-keep-tests.XXXXXXXX")"
cp -R "$TESTS" "$TESTDIR/tests"
cp "$BENCH/setup-playwright.sh" "$TESTDIR/"
if [[ -f "$BENCH/setup-playwright.cjs" ]]; then cp "$BENCH/setup-playwright.cjs" "$TESTDIR/"; fi

export PORT="${PORT:-3000}"
(cd "$OUTPUT" && bash deploy.sh) > "$OUTPUT/arc_app_startup.log" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
for i in {1..120}; do
  if curl --silent --fail "http://127.0.0.1:$PORT/" >/dev/null; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Application exited during startup; check $OUTPUT/arc_app_startup.log" >&2; exit 1
  fi
  sleep 2
done
curl --fail --silent "http://127.0.0.1:$PORT/" >/dev/null || {
  echo "Application failed readiness; check $OUTPUT/arc_app_startup.log" >&2; exit 1;
}
# Preserve logs and reports even when functional tests fail.
TEST_STATUS=0
(
  cd "$TESTDIR"
  source ./setup-playwright.sh "http://127.0.0.1:$PORT" || exit $?
  npm run test:e2e 2>&1 | tee "$OUTPUT/arc_playwright.log"
) || TEST_STATUS=$?
printf '\nOfficial Playwright run exit status: %s\n' "$TEST_STATUS"
echo "Official tests / HTML report: $TESTDIR"
if [[ -d "$TESTDIR/playwright-report" ]]; then
  cp -R "$TESTDIR/playwright-report" "$OUTPUT/arc_playwright_report"
fi
printf '%s\n' "$TESTDIR" > "$OUTPUT/arc_playwright_artifacts_path.txt"
exit "$TEST_STATUS"
