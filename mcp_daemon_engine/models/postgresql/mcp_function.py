# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for MCPFunction entity.

Mirrors the DynamoDB MCPFunctionModel schema with PostgreSQL-appropriate types.
Table: mcp_functions
"""
from __future__ import print_function

__author__ = "bibow"

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    String,
    Text,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class MCPFunctionModel(Base):
    """SQLAlchemy model for the MCPFunction entity (table: mcp_functions)."""

    __tablename__ = "mcp_functions"

    # Primary key: composite (partition_key, name)
    partition_key = Column(String(128), nullable=False, primary_key=True)
    name = Column(String, nullable=False, primary_key=True)

    # Attributes
    mcp_type = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    data = Column(JSONB, nullable=True)
    annotations = Column(Text, nullable=True)
    module_name = Column(String, nullable=True)
    class_name = Column(String, nullable=True)
    function_name = Column(String, nullable=True)
    return_type = Column(String, nullable=True)
    is_async = Column(Boolean, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))

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
        # LSI equivalent: mcp_type-index
        Index(
            "idx_mcp_functions_partition_mcp_type",
            "partition_key",
            "mcp_type",
        ),
    )


__all__ = ["MCPFunctionModel"]