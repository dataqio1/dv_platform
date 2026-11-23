"""Functional test (real API, real Oracle+Snowflake, NO mocks) for the systematic-column guard.

Scenario: Oracle DATAQ.DQ_BENCH_10M  vs  Snowflake DQ_BENCH_10M_SYS (UPDATED_DT shifted on EVERY
row → one 100%-systematic column; all other columns identical). Filtered to ID<=50000 for speed.

  A. default run        -> status 'needs_decision', decision_required.columns == ['UPDATED_DT']
  B. skip the column    -> status 'succeeded', match True, 0 value diffs (fast path)
  C. acknowledge (full) -> status 'succeeded', match False, 50000 value diffs

Run (API server must be up):  .venv/bin/python scripts/test_systematic_guard.py
"""
from __future__ import annotations
import json, sys, urllib.request

API="http://127.0.0.1:8000/api/v1"
ORA="16a1a10c-a53f-4937-b73e-8b010330488a"
SF ="3ba56de1-5484-49a6-8f65-a4a71d39babe"
PROJECT="3cca2d8d-3f88-445c-9102-e73a6a9a4113"
FILTER="ID <= 50000"

def req(method, path, body=None, timeout=180):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(API+path, data=data, method=method,
                             headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read()).get("data")

def summary_of(job_id):
    d=req("GET", f"/compares/{job_id}")
    return d["status"], (d.get("result") or {}).get("summary") or {}

def main():
    # create the test case
    job=req("POST","/compares",{
        "kind":"data","name":"TEST systematic-column guard","project_id":PROJECT,
        "source":{"connection_id":ORA,"schema":"DATAQ","table":"DQ_BENCH_10M","filter":FILTER},
        "target":{"connection_id":SF,"database":"DATAQ_BENCH","schema":"PUBLIC","table":"DQ_BENCH_10M_SYS","filter":FILTER},
        "options":{"key":["ID"]},
    })
    jid=job["id"]; print(f"created job {jid[:8]}")
    fails=[]
    def check(name, cond, got):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} — {got}")
        if not cond: fails.append(name)

    # A. default -> needs_decision
    req("PATCH", f"/compares/{jid}", {"options":{"key":["ID"]}})
    req("POST", f"/compares/{jid}:run")
    st, sm = summary_of(jid)
    dr = sm.get("decision_required") or {}
    print("\nA. default run")
    check("status is needs_decision", st=="needs_decision", f"status={st}")
    check("decision flags UPDATED_DT", dr.get("columns")==["UPDATED_DT"], f"columns={dr.get('columns')}")
    check("offers a skip rerun", "ignore_columns" in (dr.get("rerun_skip") or {}),
          f"rerun_skip={dr.get('rerun_skip')}")

    # B. skip the column -> match
    req("PATCH", f"/compares/{jid}", {"options":{"key":["ID"],"ignore_columns":["UPDATED_DT"]}})
    req("POST", f"/compares/{jid}:run")
    st, sm = summary_of(jid)
    print("\nB. skip UPDATED_DT")
    check("status succeeded", st=="succeeded", f"status={st}")
    check("match True", sm.get("match") is True, f"match={sm.get('match')}")
    check("zero value diffs", sm.get("value_difference_rows")==0, f"diffs={sm.get('value_difference_rows')}")

    # C. acknowledge -> full compare finds all diffs
    req("PATCH", f"/compares/{jid}", {"options":{"key":["ID"],"ignore_columns":[],"acknowledge_systematic":True}})
    req("POST", f"/compares/{jid}:run")
    st, sm = summary_of(jid)
    print("\nC. acknowledge (compare all)")
    check("status succeeded", st=="succeeded", f"status={st}")
    check("match False", sm.get("match") is False, f"match={sm.get('match')}")
    check("50000 value diffs", sm.get("value_difference_rows")==50000, f"diffs={sm.get('value_difference_rows')}")

    print(f"\n{'ALL PASSED ✅' if not fails else 'FAILURES: '+', '.join(fails)}")
    sys.exit(1 if fails else 0)

if __name__=="__main__":
    main()
