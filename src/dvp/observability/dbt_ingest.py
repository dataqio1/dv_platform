"""Turn a dbt run_results artifact into incidents.

A failed test or a model error opens one incident. The same node failing again
updates that incident. A pass resolves it. A skipped node is left alone, because
a test that did not run is not a pass and not a failure.
"""

from __future__ import annotations

from dvp.core.errors import ValidationError
from dvp.core.tenancy import TenantContext
from dvp.db.session import MetadataStore
from dvp.observability.incidents import IncidentService
from dvp.services import audit

_PASS = {"pass", "success"}
_FAIL = {"fail", "error", "warn"}
_SKIP = {"skipped"}


class DbtIngestService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store
        self.incidents = IncidentService(store)

    def ingest(self, ctx: TenantContext, body: dict) -> dict:
        results = body.get("results")
        if not isinstance(results, list):
            raise ValidationError("run_results has no results list")
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        invocation_id = body.get("invocation_id") or meta.get("invocation_id")
        opened = updated = resolved = skipped = ignored = 0
        failed = passed = 0
        touched: list[dict] = []
        for raw in results:
            if not isinstance(raw, dict) or not raw.get("unique_id"):
                ignored += 1
                continue
            status = str(raw.get("status") or "").lower()
            unique_id = str(raw["unique_id"])
            key = f"dbt:{unique_id}"
            if status in _SKIP:
                skipped += 1
                continue
            if status in _PASS:
                passed += 1
                if self.incidents.resolve_by_key(ctx, key):
                    resolved += 1
                continue
            if status not in _FAIL:
                ignored += 1
                continue
            failed += 1
            kind = "dbt_test" if unique_id.startswith("test.") else "dbt_run"
            severity = "critical" if status == "error" else "warning"
            name = _node_name(unique_id)
            table = _table_ref(raw, unique_id)
            message = str(raw.get("message") or "").strip()
            summary = _summary(kind, name, table, status, message)
            inc = self.incidents.open_or_update(
                ctx,
                monitor_id=None,
                dedup_key=key,
                kind=kind,
                dimension="quality" if kind == "dbt_test" else "freshness",
                severity=severity,
                summary=summary,
                table_ref=table,
                evidence={
                    "unique_id": unique_id,
                    "status": status,
                    "message": message[:500],
                    "failures": raw.get("failures"),
                    "invocation_id": invocation_id,
                },
            )
            if inc.get("occurrences") == 1:
                opened += 1
            else:
                updated += 1
            touched.append({"id": inc["id"], "summary": inc["summary"], "status": inc["status"]})
        with self.store.session(ctx) as s:
            audit.record(
                s,
                ctx,
                "dbt.ingest",
                entity_type="dbt_run",
                entity_id=str(invocation_id) if invocation_id else None,
                detail={
                    "results": len(results),
                    "failed": failed,
                    "passed": passed,
                    "skipped": skipped,
                    "opened": opened,
                    "resolved": resolved,
                },
            )
        return {
            "invocation_id": invocation_id,
            "results": len(results),
            "failed": failed,
            "passed": passed,
            "skipped": skipped,
            "ignored": ignored,
            "opened": opened,
            "updated": updated,
            "resolved": resolved,
            "incidents": touched,
        }


def _node_name(unique_id: str) -> str:
    parts = unique_id.split(".")
    if len(parts) >= 3 and parts[0] in {"test", "model", "seed", "snapshot"}:
        return parts[2]
    return parts[-1]


def _table_ref(raw: dict, unique_id: str) -> str | None:
    explicit = raw.get("table_ref") or raw.get("table")
    if explicit:
        return str(explicit)[:512]
    relation = str(raw.get("relation_name") or "")
    if relation and "dbt_test__audit" not in relation.lower():
        cleaned = relation.replace('"', "").replace("`", "")
        if cleaned:
            return cleaned[:512]
    if unique_id.startswith("model."):
        return _node_name(unique_id)
    return None


def _summary(kind: str, name: str, table: str | None, status: str, message: str) -> str:
    noun = "test" if kind == "dbt_test" else "model"
    where = f" on {table.split('.')[-1]}" if table else ""
    verb = "errored" if status == "error" else "failed"
    head = f"dbt {noun} {name} {verb}{where}"
    if message:
        return f"{head}: {message[:240]}"
    return head
