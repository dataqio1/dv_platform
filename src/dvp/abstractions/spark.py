"""SparkSessionProvider — lazy PySpark session for scale-out data compares.

Observability never needs Spark; pyspark is an optional dependency (extra: `spark`) and
importing it is deferred so the platform boots without it. The session is configured for
local mode with the JVM `--add-opens` flags Spark 3.5 needs on Java 17+/21.
"""

from __future__ import annotations

from typing import Any

# Spark 3.5 on Java 17+/21 requires these module opens or it crashes with
# InaccessibleObjectException during serialization.
_JAVA17_OPENS = " ".join(
    f"--add-opens=java.base/{pkg}=ALL-UNNAMED"
    for pkg in (
        "java.lang",
        "java.lang.invoke",
        "java.lang.reflect",
        "java.io",
        "java.net",
        "java.nio",
        "java.util",
        "java.util.concurrent",
        "java.util.concurrent.atomic",
        "sun.nio.ch",
        "sun.security.action",
        "jdk.internal.ref",
    )
)


class SparkSessionProvider:
    def __init__(self, master: str = "local[*]", app_name: str = "dv-platform") -> None:
        self.master = master
        self.app_name = app_name
        self._session: Any | None = None

    def available(self) -> bool:
        try:
            import pyspark  # noqa: F401
        except ImportError:
            return False
        return True

    def get(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            from pyspark.sql import SparkSession  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "PySpark is not installed. Install the 'spark' extra: "
                "pip install 'dv-platform[spark]'"
            ) from exc
        self._session = (
            SparkSession.builder.master(self.master)
            .appName(self.app_name)
            .config("spark.driver.extraJavaOptions", _JAVA17_OPENS)
            .config("spark.executor.extraJavaOptions", _JAVA17_OPENS)
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "8")
            .config("spark.sql.session.timeZone", "UTC")
            .getOrCreate()
        )
        self._session.sparkContext.setLogLevel("ERROR")
        return self._session
