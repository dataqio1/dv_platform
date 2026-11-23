"""One table observation: row count, schema, freshness watermark, quality score.

Shared by the manual profile path and the monitor runner so a scan records the
same facts either way. A configured check that cannot be measured is a named
gap, not a silent skip.
"""

from __future__ import annotations

from dvp.connectors.base import TableRef


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def observe_check(  # noqa: ANN001
    connector, table: TableRef, watermark: str | None, freshness_source: str = "auto"
) -> dict:
    """Scheduled check. Freshness is MAX(watermark) or newest file arrival. Volume is a
    metadata count when the warehouse has one, otherwise COUNT(*). Null rates are skipped."""
    cols = list(connector.get_schema(table))
    observation: dict = {
        "columns": [{"name": c.name, "data_type": c.data_type} for c in cols],
        "query_shape": "check",
        "queries": ["information_schema"],
    }
    count, shape = _cheap_count(connector, table)
    observation["row_count"] = count
    observation["queries"].append(shape)
    _freshness(connector, table, watermark, cols, observation, freshness_source)
    return observation


def observe_table(  # noqa: ANN001
    connector, table: TableRef, watermark: str | None, freshness_source: str = "auto"
) -> dict:
    """Full column profile. Scans the table for a row count and a null rate per column."""
    cols = list(connector.get_schema(table))
    observation: dict = {
        "columns": [{"name": c.name, "data_type": c.data_type} for c in cols],
        "query_shape": "full_profile",
        "queries": ["information_schema", "count_star_and_null_rates"],
    }
    table_sql = connector.sql_ref(table) if hasattr(connector, "sql_ref") else table.fqn()

    if cols:
        try:
            selects = ["COUNT(*) AS n"] + [
                f"SUM(CASE WHEN {_q(c.name)} IS NULL THEN 1 ELSE 0 END)" for c in cols
            ]
            row = connector.execute(f"SELECT {', '.join(selects)} FROM {table_sql}").rows[0]
            total = int(row[0] or 0)
            observation["row_count"] = total
            rates = []
            for i, _c in enumerate(cols):
                nulls = int(row[i + 1] or 0)
                rate = (nulls / total) if total else 0.0
                rates.append(rate)
                observation["columns"][i]["null_rate"] = round(rate, 6)
            if rates:
                observation["quality_score"] = round(100 * (1 - sum(rates) / len(rates)), 2)
        except NotImplementedError:
            observation["row_count"] = connector.count(table)
            observation["quality_gap"] = "This connection cannot score null rates"
        except Exception as exc:
            observation["row_count"] = _safe_count(connector, table)
            observation["quality_gap"] = f"Could not score quality: {exc}"
    else:
        observation["row_count"] = _safe_count(connector, table)
        observation["quality_gap"] = "No columns to score"

    _freshness(connector, table, watermark, cols, observation, freshness_source)
    return observation


def _cheap_count(connector, table: TableRef) -> tuple[int | None, str]:  # noqa: ANN001
    if hasattr(connector, "metadata_row_count"):
        try:
            estimated = connector.metadata_row_count(table)
        except Exception:
            estimated = None
        if estimated is not None:
            return int(estimated), "metadata_row_count"
    try:
        return connector.count(table), "count_star"
    except Exception:
        return None, "count_star"


def _freshness(connector, table, watermark, cols, observation, source="auto") -> None:  # noqa: ANN001
    """Measure freshness as either MAX(watermark_column) or the newest file's arrival time.

    `source`: 'watermark' forces the column; 'file_arrival' forces file mtime; 'auto' uses
    the watermark when one is set, else file arrival when the connector can report it (files)."""
    source = (source or "auto").lower()
    use_file = source == "file_arrival" or (source == "auto" and not watermark)
    if use_file and hasattr(connector, "latest_file_arrival"):
        _file_arrival(connector, table, observation)
        return
    if not watermark:
        if source == "file_arrival":
            observation["freshness_gap"] = "This connection cannot report file arrival times"
        return
    names = {c.name.upper(): c.name for c in cols}
    actual = names.get(watermark.upper())
    if actual is None:
        observation["freshness_gap"] = f"{watermark} is not a column on this table"
    elif not hasattr(connector, "latest_watermark"):
        observation["freshness_gap"] = f"This connection cannot read watermark {watermark}"
    else:
        observation["queries"] = list(observation.get("queries") or []) + ["max_watermark"]
        try:
            last = connector.latest_watermark(table, actual)
        except Exception as exc:
            observation["freshness_gap"] = f"Could not read {watermark}: {exc}"
        else:
            if last is None:
                observation["freshness_gap"] = f"{watermark} has no values"
            else:
                observation["last_load_ts"] = (
                    last
                    if isinstance(last, str)
                    else last.isoformat()
                    if hasattr(last, "isoformat")
                    else str(last)
                )


def _file_arrival(connector, table, observation) -> None:  # noqa: ANN001
    """Freshness from the newest backing file's last-modified time (S3/local files)."""
    observation["freshness_source"] = "file_arrival"
    observation["queries"] = list(observation.get("queries") or []) + ["file_arrival"]
    try:
        last = connector.latest_file_arrival(table)
    except Exception as exc:
        observation["freshness_gap"] = f"Could not read file arrival time: {exc}"
        return
    if last is None:
        observation["freshness_gap"] = "No backing file to read an arrival time from"
    else:
        observation["last_load_ts"] = last.isoformat() if hasattr(last, "isoformat") else str(last)


def _safe_count(connector, table: TableRef):  # noqa: ANN001
    try:
        return connector.count(table)
    except Exception:
        return None
