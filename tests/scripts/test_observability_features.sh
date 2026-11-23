#!/usr/bin/env bash
# Functional test: watermark detection/validation + four-pillar table observability.
#
# Makes REAL HTTP calls against a running dvp server (no mocks, real SQLite-backed
# service → API → DB → response). By default it boots its own server on an ephemeral
# port and tears it down; point BASE_URL at an already-running instance to skip that.
#
#   tests/scripts/test_observability_features.sh
#   BASE_URL=http://app.vexdata.io tests/scripts/test_observability_features.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PY="$REPO_ROOT/.venv/bin/python"
PORT="${PORT:-8077}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
API="$BASE_URL/api/v1"
SERVER_PID=""
PASS=0
FAIL=0

cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT

# jget <json> <python-expression over variable d>  -> prints result
jget() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print($2)" <<<"$1"; }

check() { # check <label> <actual> <expected>
  if [[ "$2" == "$3" ]]; then echo "  ✅ $1"; PASS=$((PASS+1));
  else echo "  ❌ $1 — expected [$3] got [$2]"; FAIL=$((FAIL+1)); fi
}

# ----------------------------- boot server (if local) -----------------------------
if [[ "$BASE_URL" == "http://127.0.0.1:$PORT" ]]; then
  echo "▶ starting server on :$PORT"
  PYTHONPATH="$REPO_ROOT/src" DVP_API_PORT="$PORT" "$REPO_ROOT/.venv/bin/dvp" run >/tmp/dvp_functest.log 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 30); do
    curl -sf "$API/health" >/dev/null 2>&1 && break
    sleep 0.3
  done
fi
curl -sf "$API/health" >/dev/null || { echo "server not reachable at $BASE_URL"; cat /tmp/dvp_functest.log 2>/dev/null; exit 1; }
echo "▶ server healthy at $BASE_URL"

# ----------------------------- fixtures -----------------------------
COORDS='{"database":"RAW_SALES","schema":"PUBLIC","tables":{
  "RAW_CUSTOMERS":{"columns":[
    {"name":"ID","data_type":"NUMBER","nullable":false},
    {"name":"INGESTED_AT","data_type":"TIMESTAMP_NTZ","nullable":false},
    {"name":"EMAIL","data_type":"VARCHAR","nullable":true}],
    "row_count":5000,"watermarks":{"INGESTED_AT":"2026-09-18T10:45:00"}},
  "RAW_ORDERS":{"columns":[
    {"name":"ORDER_ID","data_type":"NUMBER","nullable":false},
    {"name":"INGESTED_AT","data_type":"TIMESTAMP_NTZ","nullable":false}],
    "row_count":12000,"watermarks":{"INGESTED_AT":"2026-09-20T02:00:00"}}}}'

CONN=$(curl -s -X POST "$API/connections" -H 'content-type: application/json' \
  -d "{\"source_type\":\"inline\",\"name\":\"raw\",\"auth_method\":\"none\",\"coordinates\":$COORDS}")
CID=$(jget "$CONN" "d['data']['id']")
echo "▶ inline connection: $CID"

# ----------------------------- Feature 1: watermark -----------------------------
echo "── Feature 1: watermark detect/validate"
DET=$(curl -s -X POST "$API/connections/$CID/watermark:detect" -H 'content-type: application/json' \
  -d '{"scope":{"tables":["RAW_CUSTOMERS","RAW_ORDERS"]}}')
check "detect suggests INGESTED_AT" "$(jget "$DET" "d['data']['suggested']['column']")" "INGESTED_AT"
check "detect covers all tables"    "$(jget "$DET" "d['data']['suggested']['covers_all_tables']")" "True"

VOK=$(curl -s -X POST "$API/connections/$CID/watermark:validate" -H 'content-type: application/json' \
  -d '{"scope":{"tables":["RAW_CUSTOMERS","RAW_ORDERS"]},"column":"INGESTED_AT","expected_type":"timestamp"}')
check "validate valid=true"   "$(jget "$VOK" "d['data']['valid']")" "True"
check "validate tables_ok=2"  "$(jget "$VOK" "d['data']['tables_ok']")" "2"

VBAD=$(curl -s -X POST "$API/connections/$CID/watermark:validate" -H 'content-type: application/json' \
  -d '{"scope":{"tables":["RAW_CUSTOMERS","RAW_ORDERS"]},"column":"EMAIL"}')
check "validate missing column valid=false" "$(jget "$VBAD" "d['data']['valid']")" "False"
check "validate reports missing table"      "$(jget "$VBAD" "'RAW_ORDERS' in d['data']['missing_in']")" "True"

# ----------------------------- Feature 2: four pillars -----------------------------
echo "── Feature 2: four-pillar table observability"
MON=$(curl -s -X POST "$API/monitors" -H 'content-type: application/json' \
  -d "{\"connection_id\":\"$CID\",\"name\":\"raw-sales\",\"scope\":{\"database\":\"RAW_SALES\",\"schema\":\"PUBLIC\",\"tables\":[\"RAW_CUSTOMERS\"]},\"watermark_column\":\"INGESTED_AT\",\"watermark_type\":\"timestamp\"}")
MID=$(jget "$MON" "d['data']['id']")
check "monitor stores watermark" "$(jget "$MON" "d['data']['watermark_column']")" "INGESTED_AT"
TABLE="RAW_SALES.PUBLIC.RAW_CUSTOMERS"

# profile from the live (inline) connection
PROF=$(curl -s -X POST "$API/monitors/$MID/tables/$TABLE:profile")
check "profile records volume+schema+freshness" \
  "$(jget "$PROF" "sorted(set(d['data']['pillars_recorded']))==['freshness','schema','volume']")" "True"

PIL=$(curl -s "$API/monitors/$MID/tables/$TABLE/pillars")
check "pillar volume headline" "$(jget "$PIL" "next(c['headline'] for c in d['data']['pillars'] if c['pillar']=='volume')")" "5,000 rows"
check "pillar schema columns"  "$(jget "$PIL" "next(c['headline'] for c in d['data']['pillars'] if c['pillar']=='schema')")" "3 columns"
check "freshness configured"   "$(jget "$PIL" "next(c['configured'] for c in d['data']['pillars'] if c['pillar']=='freshness')")" "True"
check "lineage SOURCE node"    "$(jget "$PIL" "d['data']['lineage']['nodes'][0]['type']")" "SOURCE"

# add quality + column metrics via explicit snapshots → history
for i in 1 2 3; do
  NR=$(awk "BEGIN{print 0.01*$i}")
  curl -s -X POST "$API/monitors/$MID/tables/$TABLE/snapshot" -H 'content-type: application/json' \
    -d "{\"row_count\":$((4900+i*10)),\"quality_score\":9$i,\"columns\":[{\"name\":\"EMAIL\",\"data_type\":\"STRING\",\"null_rate\":$NR}],\"ts\":\"2026-09-1${i}T00:00:00Z\"}" >/dev/null
done

VH=$(curl -s "$API/monitors/$MID/tables/$TABLE/pillars/volume/history")
check "volume history has points" "$(jget "$VH" "len(d['data']['points'])>=3")" "True"
check "volume history chronological" "$(jget "$VH" "d['data']['points']==sorted(d['data']['points'], key=lambda p: p['ts'])")" "True"

COLS=$(curl -s "$API/monitors/$MID/tables/$TABLE/columns")
check "columns include EMAIL" "$(jget "$COLS" "any(c['column']=='EMAIL' for c in d['data'])")" "True"

CH=$(curl -s "$API/monitors/$MID/tables/$TABLE/columns/EMAIL/history")
check "column null_rate history grows" \
  "$(jget "$CH" "[p['value'] for p in d['data']['metrics']['quality.null_rate']]==sorted(p['value'] for p in d['data']['metrics']['quality.null_rate'])")" "True"

# ----------------------------- Feature 3: custom DQ rules (C1) -----------------------------
echo "── Feature 3: custom data quality rules + metrics"
RULE=$(curl -s -X POST "$API/monitors/$MID/rules" -H 'content-type: application/json' \
  -d "{\"name\":\"no_negative_amount\",\"type\":\"custom_sql\",\"table_ref\":\"$TABLE\",\"severity\":\"critical\",\"config\":{\"expression\":\"SELECT COUNT(*) AS v FROM {{table}} WHERE AMOUNT < 0\",\"threshold\":0,\"dimension\":\"quality\"}}")
RID=$(jget "$RULE" "d['data']['id']")
check "custom rule created" "$([ -n "$RID" ] && echo yes)" "yes"
for pair in "2026-10-01T00:00:00Z:0" "2026-10-02T00:00:00Z:0" "2026-10-03T00:00:00Z:5"; do
  TS=${pair%:*}; V=${pair##*:}
  curl -s -X POST "$API/monitors/$MID/rules/$RID:record" -H 'content-type: application/json' \
    -d "{\"table_ref\":\"$TABLE\",\"metric_value\":$V,\"ts\":\"$TS\"}" >/dev/null
done
RH=$(curl -s "$API/monitors/$MID/rules/$RID/history")
check "rule history chronological" "$(jget "$RH" "[p['value'] for p in d['data']['points']]==[0.0,0.0,5.0]")" "True"
check "rule pass/fail derived"     "$(jget "$RH" "[p['passed'] for p in d['data']['points']]==[True,True,False]")" "True"
RP=$(curl -s "$API/monitors/$MID/tables/$TABLE/rules")
check "rule shows in table panel"  "$(jget "$RP" "any(r['id']=='$RID' and r['latest']['passed']==False for r in d['data'])")" "True"

# ----------------------------- Feature 4: pushdown evaluator + run (C2) -----------------------------
echo "── Feature 4: pushdown rule evaluator + monitor run (real SQL)"
LCOORDS='{"database":"RAW_SALES","schema":"PUBLIC","tables":{"RAW_ORDERS":{"columns":[
  {"name":"ORDER_ID","data_type":"NUMBER","nullable":false},
  {"name":"AMOUNT","data_type":"NUMBER","nullable":false},
  {"name":"EMAIL","data_type":"VARCHAR","nullable":true},
  {"name":"INGESTED_AT","data_type":"TIMESTAMP_NTZ","nullable":false}],
  "rows":[
    {"ORDER_ID":1,"AMOUNT":10,"EMAIL":"a@x.com","INGESTED_AT":"2026-10-01T00:00:00"},
    {"ORDER_ID":2,"AMOUNT":-5,"EMAIL":null,"INGESTED_AT":"2026-10-02T00:00:00"},
    {"ORDER_ID":3,"AMOUNT":20,"EMAIL":"c@x.com","INGESTED_AT":"2026-10-03T00:00:00"}]}}}'
LCONN=$(curl -s -X POST "$API/connections" -H 'content-type: application/json' \
  -d "{\"source_type\":\"local\",\"name\":\"orders\",\"auth_method\":\"none\",\"coordinates\":$LCOORDS}")
LCID=$(jget "$LCONN" "d['data']['id']")
LMON=$(curl -s -X POST "$API/monitors" -H 'content-type: application/json' \
  -d "{\"connection_id\":\"$LCID\",\"name\":\"orders\",\"scope\":{\"database\":\"RAW_SALES\",\"schema\":\"PUBLIC\",\"tables\":[\"RAW_ORDERS\"]},\"watermark_column\":\"INGESTED_AT\"}")
LMID=$(jget "$LMON" "d['data']['id']")
LT=RAW_SALES.PUBLIC.RAW_ORDERS
NEG='{"name":"no_negative_amount","type":"custom_sql","table_ref":"'"$LT"'","severity":"critical","config":{"expression":"SELECT COUNT(*) AS v FROM {{table}} WHERE \"AMOUNT\" < 0","threshold":0}}'
curl -s -X POST "$API/monitors/$LMID/rules" -H 'content-type: application/json' -d "$NEG" >/dev/null
curl -s -X POST "$API/monitors/$LMID/rules" -H 'content-type: application/json' \
  -d "{\"name\":\"email_not_null\",\"type\":\"not_null\",\"columns\":[\"EMAIL\"],\"table_ref\":\"$LT\"}" >/dev/null
RUN=$(curl -s -X POST "$API/monitors/$LMID:run")
check "run evaluated 2 rules" "$(jget "$RUN" "d['data']['rules_evaluated']")" "2"
LPIL=$(curl -s "$API/monitors/$LMID/tables/$LT/pillars")
check "volume from real SQL"  "$(jget "$LPIL" "next(c['headline'] for c in d['data']['pillars'] if c['pillar']=='volume')")" "3 rows"
check "quality score computed" "$(jget "$LPIL" "next(c['configured'] for c in d['data']['pillars'] if c['pillar']=='quality')")" "True"
LRULES=$(curl -s "$API/monitors/$LMID/tables/$LT/rules")
check "custom rule failed (1 negative)" "$(jget "$LRULES" "next(r['latest']['metric_value'] for r in d['data'] if r['name']=='no_negative_amount')")" "1.0"
check "not_null rule failed (1 null)"   "$(jget "$LRULES" "next(r['latest']['passed'] for r in d['data'] if r['name']=='email_not_null')")" "False"

# ----------------------------- summary -----------------------------
echo ""
echo "════════════════════════════════════════"
echo "  PASSED: $PASS   FAILED: $FAIL"
echo "════════════════════════════════════════"
[[ "$FAIL" -eq 0 ]] || exit 1
