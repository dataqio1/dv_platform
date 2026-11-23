"""Runtime variables for test cases.

A test-case definition (source/target/options) may contain ``{{name}}`` placeholders in
any string — table, schema, SQL, filter, threshold, key, etc. Values are supplied at run
time and substituted before execution; the resolved values are recorded on the execution
so a user can always see exactly which variables produced a given result.

Example: source.table = ``{{env}}_ORDERS``, options.filter = ``LOAD_DT = '{{run_date}}'``
run with ``{"env": "PROD", "run_date": "2026-10-01"}``.
"""

from __future__ import annotations

import re
from typing import Any

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}")


def extract_variables(obj: Any) -> set[str]:
    """Every ``{{var}}`` referenced anywhere in a (possibly nested) definition."""
    out: set[str] = set()

    def walk(o: Any) -> None:
        if isinstance(o, str):
            out.update(_VAR.findall(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return out


def substitute(obj: Any, variables: dict) -> Any:
    """Return a deep copy of `obj` with every ``{{var}}`` replaced from `variables`.

    Unknown variables are left untouched (so `missing_variables` can report them)."""

    def repl(s: str) -> str:
        return _VAR.sub(
            lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0), s
        )

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return repl(o)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return walk(obj)


def missing_variables(obj: Any, variables: dict) -> list[str]:
    """Variables referenced but not supplied."""
    return sorted(extract_variables(obj) - set(variables))
