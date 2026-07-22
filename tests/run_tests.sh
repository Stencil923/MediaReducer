#!/usr/bin/env bash
# MediaReducer test suite.
#
#   tests/run_tests.sh              # unit + parity (hermetic, no network, no browser)
#   tests/run_tests.sh --integration  # + the full run pipeline over real HTTP under
#                                     # every server profile (Plex/Jellyfin/both).
#                                     # Plain fetch, no browser — needs python + node.
#   tests/run_tests.sh --e2e        # everything --integration does, plus the browser
#                                   # page tests (needs playwright + chromium).
#
# Env (all optional):
#   PLAYWRIGHT_MODULE  path/specifier for `import('playwright')` in the browser tests
#   PW_CHROMIUM        explicit chromium executable for playwright
#   MR_E2E_PORT        base app port for the opt-in tiers (default 5057; +1/+2 used)
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

# ── Integration + browser tiers (opt-in) ──
# Two tiers share the same booted app + mocks:
#   --integration : the full run pipeline (scan→score→queue) over real HTTP,
#                   under EVERY server profile (Plex / Jellyfin / both). Plain
#                   fetch, NO browser — needs only python + node (already
#                   required for parity).
#   --e2e         : everything --integration does, PLUS the browser page tests
#                   (needs playwright + chromium; see the env vars above).
MODE="${1:-}"
if [[ "$MODE" == "--integration" || "$MODE" == "--e2e" ]]; then
  export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"

  # Boot an app instance against a fixture config on a port; wait for health.
  boot_app() { # <config> <library> <port>  -> echoes the app PID (only)
    # Redirect the app's own output to a log so command substitution captures
    # ONLY the echoed PID, not Flask's startup chatter.
    MEDIAREDUCER_CONFIG="$1" MEDIAREDUCER_LIBRARY="$2" MEDIAREDUCER_PORT="$3" \
      python3 - >"$TMP/app-$3.log" 2>&1 <<PY &
import os, sys
sys.path.insert(0, "$REPO")
import app
app.app.run(host="127.0.0.1", port=int(os.environ["MEDIAREDUCER_PORT"]))
PY
    local pid=$! url="http://127.0.0.1:$3"
    for _ in $(seq 1 40); do
      curl -sf "$url/api/status" >/dev/null 2>&1 && break
      sleep 0.5
    done
    echo "$pid"
  }

  PORT="${MR_E2E_PORT:-5057}"
  # Shared mocks: Tautulli (Plex source) and Jellyfin serve the SAME library.
  python3 tests/mocks/mock_tautulli.py 8765 & MOCK_PID=$!
  python3 tests/mocks/mock_jellyfin.py 8767 & JF_MOCK_PID=$!

  # ── Full run pipeline under each server profile (fetch, no browser) ──
  # Plex runs a second Simulate to prove the metadata-cache-reuse path; Jellyfin
  # and both run once (Jellyfin metadata isn't cached the same way, so a second
  # run there just repeats the first — MR_E2E_SECOND_RUN=0 skips it).
  python3 tests/fixtures/make_fixtures.py "$TMP/e2e" plex >/dev/null
  export MEDIAREDUCER_LIBRARY="$TMP/e2e/library"
  export MEDIAREDUCER_CONFIG="$TMP/e2e/config/config.json"
  export MR_BASE_URL="http://127.0.0.1:$PORT"
  APP_PID="$(boot_app "$MEDIAREDUCER_CONFIG" "$TMP/e2e/library" "$PORT")"
  run e2e_fullrun_plex node tests/e2e/e2e_fullrun.mjs

  python3 tests/fixtures/make_fixtures.py "$TMP/e2e-jf" jellyfin >/dev/null
  JF_PORT=$((PORT+1))
  JF_APP_PID="$(boot_app "$TMP/e2e-jf/config/config.json" "$TMP/e2e-jf/library" "$JF_PORT")"
  MR_BASE_URL="http://127.0.0.1:$JF_PORT" MR_E2E_SECOND_RUN=0 \
    run e2e_fullrun_jellyfin node tests/e2e/e2e_fullrun.mjs

  python3 tests/fixtures/make_fixtures.py "$TMP/e2e-both" both >/dev/null
  BOTH_PORT=$((PORT+2))
  BOTH_APP_PID="$(boot_app "$TMP/e2e-both/config/config.json" "$TMP/e2e-both/library" "$BOTH_PORT")"
  MR_BASE_URL="http://127.0.0.1:$BOTH_PORT" MR_E2E_SECOND_RUN=0 \
    run e2e_fullrun_both node tests/e2e/e2e_fullrun.mjs

  # ── Browser page tests (only for --e2e, and only if playwright is present) ──
  # These drive chromium and are Plex-only UI checks, so they run once against
  # the already-booted Plex app.
  if [[ "$MODE" == "--e2e" ]]; then
    if node -e "import(process.env.PLAYWRIGHT_MODULE||'playwright').then(()=>process.exit(0)).catch(()=>process.exit(1))" 2>/dev/null; then
      MR_BASE_URL="http://127.0.0.1:$PORT" run e2e_smoke      node tests/e2e/smoke_all.mjs
      MR_BASE_URL="http://127.0.0.1:$PORT" run e2e_runlock    node tests/e2e/e2e_runlock.mjs
      MR_BASE_URL="http://127.0.0.1:$PORT" run e2e_debugghost   node tests/e2e/e2e_debugghost.mjs
      MR_BASE_URL="http://127.0.0.1:$PORT" run e2e_prune_confirm node tests/e2e/e2e_prune_confirm.mjs

      # A Debug-mode dashboard (its own app + isolated OUTPUT_DIR): the Live
      # button morphs to Debug Live Run, which must stay enabled through status
      # polls that report the live/safety thresholds blocked.
      DBG_OUT="$TMP/e2e-dbg-out"; mkdir -p "$DBG_OUT"
      DBG_CFG="$TMP/e2e-dbg-config.json"
      python3 - "$MEDIAREDUCER_CONFIG" "$DBG_CFG" "$DBG_OUT" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg["DEBUG_MODE"] = True
cfg["RUN_MODE"] = "paused"
cfg["OUTPUT_DIR"] = sys.argv[3]
json.dump(cfg, open(sys.argv[2], "w"))
PY
      # +33, not +3: PORT+3 (5060 by default) is SIP, which Chromium blocks as
      # an "unsafe port" (net::ERR_UNSAFE_PORT). Keep clear of reserved ports.
      DBG_PORT=$((PORT+33))
      DBG_APP_PID="$(boot_app "$DBG_CFG" "$TMP/e2e/library" "$DBG_PORT")"
      MR_BASE_URL="http://127.0.0.1:$DBG_PORT" run e2e_debuglive_btn node tests/e2e/e2e_debuglive_btn.mjs
      kill "$DBG_APP_PID" 2>/dev/null
    else
      echo "SKIP browser tests — playwright not installed (set PLAYWRIGHT_MODULE, or run: npm i playwright)"
    fi
  fi

  kill "$APP_PID" "$JF_APP_PID" "$BOTH_APP_PID" "$MOCK_PID" "$JF_MOCK_PID" 2>/dev/null
fi

echo
echo "== $pass passed, $fail failed =="
[[ $fail -gt 0 ]] && printf 'failed: %s\n' "${failed_names[@]}"
exit $((fail > 0))
