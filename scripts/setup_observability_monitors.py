"""Create + run observability monitors on the DATA_OBSERVABILITY medallion (via the real API)."""
from __future__ import annotations
import json, urllib.request

API="http://127.0.0.1:8000/api/v1"
SF="3ba56de1-5484-49a6-8f65-a4a71d39babe"
PROJECT="3cca2d8d-3f88-445c-9102-e73a6a9a4113"
DB="DATA_OBSERVABILITY"

MONS=[
 dict(name="Observability · source (ORDERS_RAW)", schema="DATA_OBSERVABILITY_SOURCE",
      table="ORDERS_RAW", wm="EVENT_TS", wt="timestamp", layer="bronze"),
 dict(name="Observability · curate (ORDERS_CURATED)", schema="DATA_OBSERVABILITY_CURATE",
      table="ORDERS_CURATED", wm="EVENT_TS", wt="timestamp", layer="silver"),
 dict(name="Observability · publish (DAILY_ORDER_SUMMARY)", schema="DATA_OBSERVABILITY_PUBLISH",
      table="DAILY_ORDER_SUMMARY", wm="ORDER_DATE", wt="date", layer="gold"),
]

def req(method, path, body=None, timeout=180):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(API+path, data=data, method=method, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"__error__": e.code, "body": e.read().decode()[:300]}

def main():
    for m in MONS:
        print(f"\n######## {m['name']} ########")
        created=req("POST","/monitors",{
            "connection_id":SF,"name":m["name"],
            "scope":{"database":DB,"schema":m["schema"],"tables":[m["table"]]},
            "watermark_column":m["wm"],"watermark_type":m["wt"],
            "load_type":"append","lookback_days":30,
            "project_id":PROJECT,"layer":m["layer"],"source_system":"orders_observability",
        })
        if "__error__" in created:
            print("  create FAILED:", created); continue
        mid=created["data"]["id"]; print(f"  created monitor {mid[:8]}")
        # run it
        run=req("POST",f"/observability/monitors/{mid}:run")
        if "__error__" in run: print("  run FAILED:", run);
        else:
            d=run["data"]
            print("  run:", json.dumps(d)[:240])
        # tables + volume pillar history
        tabs=req("GET",f"/observability/monitors/{mid}/tables")
        tref=None
        if "data" in tabs and tabs["data"]:
            t0=tabs["data"][0]; tref=t0.get("table_ref") or t0.get("ref") or t0.get("name")
            print("  table_ref:", tref)
        if tref:
            for pillar in ("volume","freshness"):
                h=req("GET",f"/observability/monitors/{mid}/tables/{urllib.parse.quote(str(tref),safe='')}/pillars/{pillar}/history")
                pts=(h.get("data") or {}) if isinstance(h.get("data"),dict) else h.get("data")
                print(f"  {pillar} history:", json.dumps(h.get('data'))[:200] if 'data' in h else h)
        inc=req("GET",f"/observability/monitors/{mid}/incidents")
        items=inc.get("data") if "data" in inc else inc
        n=len(items) if isinstance(items,list) else items
        print("  incidents:", n if isinstance(n,int) else json.dumps(items)[:200])
        if isinstance(items,list):
            for it in items[:8]:
                print("    -", it.get("pillar"), it.get("severity"), (it.get("title") or it.get("summary") or "")[:90])

if __name__=="__main__":
    import urllib.parse
    main()
