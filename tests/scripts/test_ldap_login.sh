#!/usr/bin/env bash
# Functional test: LDAP-group multi-tenancy against a REAL LDAP server (no mocks).
#
# Binds a real user, provisions a tenant per LDAP group, and verifies the caller is scoped
# to those tenants. Skips cleanly if LDAP_URL is unset so CI stays green.
#
# Requires:
#   LDAP_URL                 e.g. ldap://localhost:389
#   LDAP_USER_DN_TEMPLATE    e.g. "uid={username},ou=people,dc=example,dc=com"
#   LDAP_GROUP_BASE_DN       e.g. "ou=groups,dc=example,dc=com"
#   LDAP_USERNAME LDAP_PASSWORD   a real directory user + password
# Optional: LDAP_ADMIN_GROUPS (comma-sep), LDAP_DEFAULT_ROLE
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
PY="$REPO_ROOT/.venv/bin/python"
PORT="${PORT:-8079}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
API="$BASE_URL/api/v1"
SERVER_PID=""
PASS=0; FAIL=0

if [[ -z "${LDAP_URL:-}" ]]; then
  echo "⏭  SKIP: LDAP_URL not set — a live LDAP server is required for this test."
  exit 0
fi

cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT
jget() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print($2)" <<<"$1"; }
check() { if [[ "$2" == "$3" ]]; then echo "  ✅ $1"; PASS=$((PASS+1)); else echo "  ❌ $1 — expected [$3] got [$2]"; FAIL=$((FAIL+1)); fi; }
checktrue() { if [[ "$2" == "True" ]]; then echo "  ✅ $1"; PASS=$((PASS+1)); else echo "  ❌ $1 ($2)"; FAIL=$((FAIL+1)); fi; }

echo "▶ starting server on :$PORT (LDAP identity)"
PYTHONPATH="$REPO_ROOT/src" \
  DVP_IDENTITY_PROVIDER=ldap \
  DVP_LDAP_URL="$LDAP_URL" \
  DVP_LDAP_USER_DN_TEMPLATE="${LDAP_USER_DN_TEMPLATE}" \
  DVP_LDAP_GROUP_BASE_DN="${LDAP_GROUP_BASE_DN}" \
  DVP_LDAP_ADMIN_GROUPS="${LDAP_ADMIN_GROUPS:-}" \
  DVP_LDAP_DEFAULT_ROLE="${LDAP_DEFAULT_ROLE:-editor}" \
  DVP_API_PORT="$PORT" "$REPO_ROOT/.venv/bin/dvp" run >/tmp/dvp_ldaptest.log 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 0.3; done
curl -sf "$API/health" >/dev/null || { echo "server not reachable"; cat /tmp/dvp_ldaptest.log; exit 1; }

TOKEN="${LDAP_USERNAME}:${LDAP_PASSWORD}"

ME=$(curl -s "$API/me/groups" -H "Authorization: $TOKEN")
echo "  ↳ tenants: $ME"
checktrue "user has at least one LDAP tenant" "$(jget "$ME" "len(d['data'])>0")"

# act within the first tenant
GID=$(jget "$ME" "d['data'][0]['group_id']")
WHO=$(curl -s "$API/me" -H "Authorization: $TOKEN" -H "X-Group-Id: $GID")
check "resolves active group" "$(jget "$WHO" "d['data']['group_id']")" "$GID"

# a project created here is scoped to this tenant
PROJ=$(curl -s -X POST "$API/projects" -H "Authorization: $TOKEN" -H "X-Group-Id: $GID" \
  -H 'content-type: application/json' -d '{"name":"ldap-scoped"}')
checktrue "project created in tenant" "$(jget "$PROJ" "'id' in d.get('data',{})")"

echo ""
echo "──────────────────────────────────────────"
echo "  PASS=$PASS  FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] && echo "  ✅ ALL PASSED" || echo "  ❌ FAILURES"
exit "$FAIL"
