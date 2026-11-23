"""Out-of-process execution for heavy compares.

The metadata DB is the queue (no Redis). A `dvp worker` process claims queued compare
jobs and runs each as its OWN OS process (`dvp.workers.run_compare`), so a large
Snowflake↔Oracle / S3↔Snowflake compare is isolated from the API and can be given its own
Spark session. The Spark target is a config URL (`spark_master`) — local[*], a standalone
cluster, Spark Connect, or K8s — so scaling is a config change, not a code change.
"""
