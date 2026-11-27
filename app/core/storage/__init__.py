"""Database storage monitoring.

Owned by: core/storage. A daily snapshot (taken by the maintenance job, never per request) records:

* MEASURED facts: total database size (`pg_database_size`) and per-table total / table / index sizes
  (`pg_total_relation_size`, `pg_relation_size`, `pg_indexes_size`);
* planner ESTIMATES: per-table row counts (`pg_class.reltuples`);
* per-organization ESTIMATES: exact row counts (counted under that organization's row-level security) and an
  estimated byte share. PostgreSQL does not attribute table bytes to tenants, so the byte figure is a
  proportional estimate -- its `basis` is stored and shown with it, and it is never presented as a measurement.

The storage LIMIT is configuration (`DATABASE_STORAGE_LIMIT_BYTES`), never guessed. With no limit configured the
snapshot has no percentage and no warning.
"""
