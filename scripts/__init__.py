"""Operator/developer scripts for the intraday engine (run via ``python -m scripts.*``).

These are out-of-band utilities (data ingestion, scoped vendor pulls) — NOT part
of the importable ``intraday`` package and never invoked by the engine at runtime.
All are reads-only with respect to brokers (no orders) and paper-only.
"""
