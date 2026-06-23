# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy base metadata and shared helpers.

This module is only imported when ``DB_BACKEND=postgresql``.
DynamoDB-only installs never import SQLAlchemy.
"""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import scoped_session, sessionmaker
except ImportError:  # pragma: no cover - DynamoDB-only environments
    raise ImportError(
        "SQLAlchemy is required for PostgreSQL backend. "
        "Install with: pip install mcp-daemon-engine[postgresql]"
    )

Base = declarative_base()


def normalize_row(row: Any) -> Optional[Dict[str, Any]]:
    """Convert a SQLAlchemy model instance to a normalized dict.

    Handles UUID, datetime, JSONB, and Decimal types for JSON serialization.
    """
    if row is None:
        return None

    from ...utils.normalization import normalize_to_json

    if isinstance(row, dict):
        return normalize_to_json(row)

    # SQLAlchemy ORM object — extract column attributes
    if hasattr(row, "__table__"):
        result = {}
        for col in row.__table__.columns:
            key = col.name
            val = getattr(row, key, None)
            result[key] = _serialize_value(val)
        return normalize_to_json(result)

    return normalize_to_json(row)


def _serialize_value(val: Any) -> Any:
    """Serialize individual SQLAlchemy column values to JSON-safe types."""
    import datetime
    from decimal import Decimal
    from uuid import UUID as UUIDType

    if val is None:
        return None
    if isinstance(val, UUIDType):
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (list, dict)):
        return val
    return val


__all__ = ["Base", "normalize_row"]