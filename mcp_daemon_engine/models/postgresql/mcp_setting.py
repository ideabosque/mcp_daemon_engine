# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for MCPSetting entity.

Mirrors the DynamoDB MCPSettingModel schema with PostgreSQL-appropriate types.
Table: mcp_settings
"""
from __future__ import print_function

__author__ = "bibow"

from sqlalchemy import (
    Column,
    String,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class MCPSettingModel(Base):
    """SQLAlchemy model for the MCPSetting entity (table: mcp_settings)."""

    __tablename__ = "mcp_settings"

    # Primary key: composite (partition_key, setting_id)
    partition_key = Column(String(128), nullable=False, primary_key=True)
    setting_id = Column(String, nullable=False, primary_key=True)

    # Attributes
    setting = Column(JSONB, nullable=True)  # config blob

    # Timestamps
    updated_by = Column(String(64), nullable=False)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )


__all__ = ["MCPSettingModel"]