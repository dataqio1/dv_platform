"""Branded PDF rendering for reports (optional `pdf` extra → reportlab).

Kept isolated so the platform runs without reportlab installed; `render_pdf` raises a clear
error if the extra is missing, and the export endpoint surfaces it as a normal API error."""

from __future__ import annotations

from typing import Any


def available() -> bool:
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def render_pdf(name: str, report_type: str, doc: dict) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "PDF export needs the 'pdf' extra: pip install 'dv-platform[pdf]'"
        ) from exc

    import io

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, title=name, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    story: list[Any] = []

    story.append(Paragraph(name, styles["Title"]))
    headline = doc.get("pass_rate_pct", doc.get("health_score_pct"))
    sub = report_type.replace("_", " ").title()
    if headline is not None:
        sub += f"  ·  headline {headline}%"
    if doc.get("generated_at"):
        sub += f"  ·  {doc['generated_at']}"
    story.append(Paragraph(sub, styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    def table(headers: list[str], rows: list[list[Any]]) -> None:
        if not rows:
            return
        data = [headers] + [[_s(c) for c in r] for r in rows]
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f8fafc")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(t)
        story.append(Spacer(1, 6 * mm))

    if report_type == "data_quality":
        story.append(Paragraph("Rules", styles["Heading2"]))
        table(
            ["Rule", "Table", "Dimension", "Passed"],
            [
                [r.get("name"), r.get("table_ref"), r.get("dimension"), r.get("passed")]
                for r in doc.get("rules", [])
            ],
        )
    elif report_type == "executive":
        table(
            ["Metric", "Value"],
            [
                [k, doc.get(k)]
                for k in (
                    "health_score_pct",
                    "validation_pass_rate_pct",
                    "quality_pass_rate_pct",
                    "open_incidents",
                    "test_cases",
                    "rules",
                )
            ],
        )
    elif report_type == "profile":
        for tbl in doc.get("tables", []):
            story.append(
                Paragraph(f"{tbl.get('table')}  ({tbl.get('row_count')} rows)", styles["Heading3"])
            )
            table(
                ["Column", "Type", "Null %", "Empty", "Unique", "~", "Min", "Max"],
                [
                    [
                        c.get("name"),
                        c.get("data_type"),
                        c.get("null_pct"),
                        c.get("empty_count"),
                        c.get("distinct"),
                        "~" if c.get("distinct_approx") else "",
                        c.get("min"),
                        c.get("max"),
                    ]
                    for c in tbl.get("columns", [])
                ],
            )
    else:  # data_validation
        story.append(Paragraph("Test cases", styles["Heading2"]))
        table(
            ["Test case", "Kind", "Source", "Target", "Status"],
            [
                [c.get("name"), c.get("kind"), c.get("source"), c.get("target"), c.get("status")]
                for c in doc.get("test_cases", [])
            ],
        )

    pdf.build(story)
    return buf.getvalue()


def _s(v: Any) -> str:
    return "" if v is None else str(v)
