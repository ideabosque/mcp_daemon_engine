# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for MCPModule entity.

Mirrors the DynamoDB MCPModuleModel schema with PostgreSQL-appropriate types.
Table: mcp_modules
"""
from __future__ import print_function

__author__ = "bibow"

from sqlalchemy import (
    Column,
    Index,
    String,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class MCPModuleModel(Base):
    """SQLAlchemy model for the MCPModule entity (table: mcp_modules)."""

    __tablename__ = "mcp_modules"

    # Primary key: composite (partition_key, module_name)
    partition_key = Column(String(128), nullable=False, primary_key=True)
    module_name = Column(String, nullable=False, primary_key=True)

    # Attributes
    package_name = Column(String, nullable=False)
    classes = Column(JSONB, nullable=True)  # list of {class_name, setting_id}
    source = Column(String, nullable=True)

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

    __table_args__ = (
        # LSI equivalent: package_name-index
        Index(
            "idx_mcp_modules_partition_package_name",
            "partition_key",
            "package_name",
        ),
    )


__all__ = ["MCPModuleModel"]