#!/usr/bin/env bash
# MediaReducer test suite.
#
#   tests/run_tests.sh            # unit + parity (hermetic, no network)
#   tests/run_tests.sh --e2e      # also boot mock Tautulli + the app and run
#                                 # the browser tests (needs playwright +
#                                 # chromium; see the env vars below)
#
# Env (all optional):
#   PLAYWRIGHT_MODULE  path/specifier for `import('playwright')` in the .mjs tests
#   PW_CHROMIUM        explicit chromium executable for playwright
#   MR_E2E_PORT        app port for e2e (default 5057)
set -u
cd "$(dirname "$0")/.."
REPO="$PWD"
TMP="$(mktemp -d /tmp/mediareducer-tests.XXXXXX)"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$TMP"' EXIT

pass=0; fail=0; failed_names=()
run() {
  local name="$1"; shift
  if "$@" >"$TMP/$name.log" 2>&1; then
    echo "PASS $name"; pass=$((pass+1))
  else
    echo "FAIL $name (log: $TMP/$name.log)"; tail -5 "$TMP/$name.log" | sed 's/^/    /'
    fail=$((fail+1)); failed_names+=("$name")
  fi
}

# ── Unit tests (each is a standalone script; hermetic via a temp config) ──
export MEDIAREDUCER_CONFIG="$TMP/unit-config/config.json"
mkdir -p "$TMP/unit-config"
for t in tests/unit/test_*.py; do
  run "$(basename "$t" .py)" python3 "$t"
done

# ── Scoring parity: engine vs the Score Explorer's JS mirror ──
run parity_gen python3 tests/parity/gen_py_scores.py "$TMP/parity"
run parity_check node tests/parity/parity_check.cjs "$TMP/parity"

# ── Browser e2e (opt-in) ──
if [[ "${1:-}" == "--e2e" ]]; then
 # Preflight: the .mjs tests import playwright. If it isn't installed, skip the
 # whole block cleanly rather than reporting every e2e case as a failure.
 if node -e "import(process.env.PLAYWRIGHT_MODULE||'playwright').then(()=>process.exit(0)).catch(()=>process.exit(1))" 2>/dev/null; then
  PORT="${MR_E2E_PORT:-5057}"
  python3 tests/fixtures/make_fixtures.py "$TMP/e2e" >/dev/null
  python3 tests/mocks/mock_tautulli.py 8765 & MOCK_PID=$!
  export MEDIAREDUCER_CONFIG="$TMP/e2e/config/config.json"
  export MEDIAREDUCER_LIBRARY="$TMP/e2e/library"
  export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"
  export MR_BASE_URL="http://127.0.0.1:$PORT"
  MEDIAREDUCER_PORT="$PORT" python3 - <<PY &
import os, sys
sys.path.insert(0, "$REPO")
import app
app.app.run(host="127.0.0.1", port=int(os.environ["MEDIAREDUCER_PORT"]))
PY
  APP_PID=$!
  for _ in $(seq 1 40); do
    curl -sf "$MR_BASE_URL/api/status" >/dev/null 2>&1 && break
    sleep 0.5
  done

  run e2e_smoke node tests/e2e/smoke_all.mjs
  run e2e_runlock node tests/e2e/e2e_runlock.mjs

  # Build a first sample (unrated: balance 0, no dataset in OUTPUT_DIR yet).
  curl -sf -X POST -H "Content-Type: application/json" -H "X-MediaReducer: 1" \
       -d '{"n":10}' "$MR_BASE_URL/api/score-sample/refresh" >/dev/null
  for _ in $(seq 1 60); do
    curl -sf "$MR_BASE_URL/api/score-sample/refresh/status" | grep -q '"active":false' && break
    sleep 1
  done
  run e2e_annotate_nofile node tests/e2e/e2e_annotate.mjs nofile
  cp "$TMP/e2e/ratings/title.ratings.tsv" "$TMP/e2e/config/title.ratings.tsv"
  touch -d "30 days ago" "$TMP/e2e/config/title.ratings.tsv" 2>/dev/null || true
  run e2e_annotate_file node tests/e2e/e2e_annotate.mjs file

  # Refresh-commits-dial needs the saved balance off 0 first.
  curl -sf -X POST -H "Content-Type: application/json" -H "X-MediaReducer: 1" \
       -d '{"SCORE_BALANCE":50}' "$MR_BASE_URL/api/score-config" >/dev/null
  sleep 8
  run e2e_refresh node tests/e2e/e2e_refresh.mjs

  kill "$APP_PID" "$MOCK_PID" 2>/dev/null
 else
  echo "SKIP e2e — playwright not installed (set PLAYWRIGHT_MODULE, or run: npm i playwright)"
 fi
fi

echo
echo "== $pass passed, $fail failed =="
[[ $fail -gt 0 ]] && printf 'failed: %s\n' "${failed_names[@]}"
exit $((fail > 0))
