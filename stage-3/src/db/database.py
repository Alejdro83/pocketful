"""Pocketful database connection and initialization.

Backward-compatible wrapper — imports from schema.py which owns the full schema.
"""

from src.db.schema import get_db, init_db, reset_db, DB_PATH  # noqa: F401

__all__ = ["get_db", "init_db", "reset_db", "DB_PATH"]