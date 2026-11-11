"""Schema setup. create_all makes new tables; ensure_columns adds columns to tables that
already exist (create_all does not alter them), so an upgrade keeps existing data."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .models import Base

ADDED_COLUMNS = [
    ("accounts", "household", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("transactions", "category_source", "VARCHAR(10) NOT NULL DEFAULT 'default'"),
    ("transactions", "is_transfer", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("transactions", "transfer_id", "INTEGER"),
]


def setup_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
