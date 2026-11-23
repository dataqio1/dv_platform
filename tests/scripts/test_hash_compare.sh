#!/usr/bin/env bash
# Functional test: Snowflake→Snowflake HASH-pushdown data compare (real, no mocks).
#
# Makes REAL HTTP calls against a running dvp server AND real queries against live
# Snowflake. Per the golden rule this uses no mocks — it creates two real Snowflake
# connections (key-pair auth) and runs a hash-engine data compare end to end.
#
# Requires these env vars (skips cleanly if SNOWFLAKE_ACCOUNT is unset, so CI stays green):
#   SNOWFLAKE_ACCOUNT  SNOWFLAKE_USER  SNOWFLAKE_WAREHOUSE  SNOWFLAKE_ROLE
#   SNOWFLAKE_PRIVATE_KEY_PATH            (PEM file for key-pair auth)
#   SRC_DB SRC_SCHEMA SRC_TABLE          (source table)
#   DST_DB DST_SCHEMA DST_TABLE          (target table — same account or a second one)
#   KEY_COLS                              (comma-separated key, e.g. "ORDER_ID")
# Optional second account for the target:
#   DST_SNOWFLAKE_ACCOUNT DST_SNOWFLAKE_USER DST_PRIVATE_KEY_PATH
#
#   tests/scripts/test_hash_compare.sh
#   BASE_URL=http://app.vexdata.io tests/scripts/test_hash_compare.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
PY="$REPO_ROOT/.venv/bin/python"
PORT="${PORT:-8078}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
API="$BASE_URL/api/v1"
SERVER_PID=""
PASS=0; FAIL=0

if [[ -z "${SNOWFLAKE_ACCOUNT:-}" ]]; then
  echo "⏭  SKIP: SNOWFLAKE_ACCOUNT not set — live Snowflake credentials required for this test."
  echo "    (set SNOWFLAKE_* + SRC_*/DST_* env vars to run the real end-to-end hash compare)"
  exit 0
fi

cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT
jget() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print($2)" <<<"$1"; }
check() { if [[ "$2" == "$3" ]]; then echo "  ✅ $1"; PASS=$((PASS+1)); else echo "  ❌ $1 — expected [$3] got [$2]"; FAIL=$((FAIL+1)); fi; }
checkne() { if [[ "$2" != "$3" ]]; then echo "  ✅ $1 ($2)"; PASS=$((PASS+1)); else echo "  ❌ $1 — unexpected [$2]"; FAIL=$((FAIL+1)); fi; }

# ----------------------------- boot server (if local) -----------------------------
if [[ "$BASE_URL" == "http://127.0.0.1:$PORT" ]]; then
  echo "▶ starting server on :$PORT"
  PYTHONPATH="$REPO_ROOT/src" DVP_API_PORT="$PORT" "$REPO_ROOT/.venv/bin/dvp" run >/tmp/dvp_hashtest.log 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 0.3; done
fi
curl -sf "$API/health" >/dev/null || { echo "server not reachable at $BASE_URL"; cat /tmp/dvp_hashtest.log 2>/dev/null; exit 1; }
echo "▶ server healthy at $BASE_URL"

# ----------------------------- create two Snowflake connections -----------------------------
SRC_PK=$("$PY" -c "import json,sys; print(json.dumps(open('${SNOWFLAKE_PRIVATE_KEY_PATH}').read()))")
mk_conn() { # mk_conn <name> <account> <user> <db> <schema> <pk-json>
  local coords secret
  coords=$("$PY" -c "import json;print(json.dumps({'account':'$2','user':'$3','warehouse':'${SNOWFLAKE_WAREHOUSE}','role':'${SNOWFLAKE_ROLE:-}','database':'$4','schema':'$5'}))")
  secret="{\"private_key\": $6}"
  curl -s -X POST "$API/connections" -H 'content-type: application/json' \
    -d "{\"source_type\":\"snowflake\",\"name\":\"$1\",\"auth_method\":\"key_pair\",\"coordinates\":$coords,\"secret\":$secret}"
}

SRC_CONN=$(mk_conn "sf-src" "$SNOWFLAKE_ACCOUNT" "$SNOWFLAKE_USER" "$SRC_DB" "$SRC_SCHEMA" "$SRC_PK")
SRC_ID=$(jget "$SRC_CONN" "d['data']['id']")
check "source connection created" "$(jget "$SRC_CONN" "'id' in d.get('data',{})")" "True"

DST_ACCT="${DST_SNOWFLAKE_ACCOUNT:-$SNOWFLAKE_ACCOUNT}"
DST_USER="${DST_SNOWFLAKE_USER:-$SNOWFLAKE_USER}"
DST_PK="$SRC_PK"; [[ -n "${DST_PRIVATE_KEY_PATH:-}" ]] && DST_PK=$("$PY" -c "import json;print(json.dumps(open('${DST_PRIVATE_KEY_PATH}').read()))")
DST_CONN=$(mk_conn "sf-dst" "$DST_ACCT" "$DST_USER" "$DST_DB" "$DST_SCHEMA" "$DST_PK")
DST_ID=$(jget "$DST_CONN" "d['data']['id']")
check "target connection created" "$(jget "$DST_CONN" "'id' in d.get('data',{})")" "True"

# secrets must never come back from the API
checkne "secret not echoed" "$(jget "$SRC_CONN" "d['data'].get('secret','<absent>')")" "$SRC_PK"

# ----------------------------- hash compare -----------------------------
KEY_JSON=$("$PY" -c "import json,os;print(json.dumps([c.strip() for c in os.environ['KEY_COLS'].split(',')]))")
JOB=$(curl -s -X POST "$API/compares" -H 'content-type: application/json' -d "{
  \"kind\":\"data\",\"name\":\"sf hash compare\",
  \"source\":{\"connection_id\":\"$SRC_ID\",\"table\":\"$SRC_TABLE\"},
  \"target\":{\"connection_id\":\"$DST_ID\",\"table\":\"$DST_TABLE\"},
  \"options\":{\"key\":$KEY_JSON,\"engine\":\"hash\"}}")
JOB_ID=$(jget "$JOB" "d['data']['id']")
check "compare job created" "$(jget "$JOB" "'id' in d.get('data',{})")" "True"

RUN=$(curl -s -X POST "$API/compares/$JOB_ID:run")
STATUS=$(jget "$RUN" "d['data']['status']")
check "run succeeded" "$STATUS" "succeeded"
ENGINE=$(jget "$RUN" "d['data']['result']['summary'].get('engine')")
check "executed on hash engine (pushdown)" "$ENGINE" "hash"

echo "  ↳ summary: $(jget "$RUN" "d['data']['result']['summary']")"

# differences endpoint returns buckets without moving the whole table
DIFF=$(curl -s "$API/compares/$JOB_ID/differences?limit=5")
check "differences readable" "$(jget "$DIFF" "isinstance(d.get('data'), list)")" "True"

echo ""
echo "──────────────────────────────────────────"
echo "  PASS=$PASS  FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] && echo "  ✅ ALL PASSED" || echo "  ❌ FAILURES"
exit "$FAIL"
