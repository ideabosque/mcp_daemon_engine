# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for MCPFunctionCall entity.

Mirrors the DynamoDB MCPFunctionCallModel schema with PostgreSQL-appropriate types.
Table: mcp_function_calls

S3 content offload divergence:
- DynamoDB auto-offloads content to S3 when the item exceeds 400KB.
- PostgreSQL has no such row size limit, so the PG repository only offloads
  when the caller explicitly sets content_in_s3=True.
- The PG repository still hydrates content from S3 when content_in_s3 is set
  on an existing row.
"""
from __future__ import print_function

__author__ = "bibow"

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class MCPFunctionCallModel(Base):
    """SQLAlchemy model for the MCPFunctionCall entity (table: mcp_function_calls)."""

    __tablename__ = "mcp_function_calls"

    # Primary key: composite (partition_key, mcp_function_call_uuid)
    # Note: mcp_function_call_uuid is a String (not UUID-typed) to preserve
    # the existing string semantics (uuid.uuid4() as string).
    partition_key = Column(String(128), nullable=False, primary_key=True)
    mcp_function_call_uuid = Column(String, nullable=False, primary_key=True)

    # Attributes
    name = Column(String, nullable=False)
    mcp_type = Column(String, nullable=False)
    arguments = Column(JSONB, nullable=True)
    content_in_s3 = Column(Boolean, nullable=False, default=False)
    content = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="initial")
    notes = Column(Text, nullable=True)
    time_spent = Column(Integer, nullable=True)

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
        # LSI equivalents: mcp_type-index, name-index, updated_at-index
        Index(
            "idx_mcp_function_calls_partition_mcp_type",
            "partition_key",
            "mcp_type",
        ),
        Index(
            "idx_mcp_function_calls_partition_name",
            "partition_key",
            "name",
        ),
        Index(
            "idx_mcp_function_calls_partition_updated_at",
            "partition_key",
            "updated_at",
        ),
    )


__all__ = ["MCPFunctionCallModel"]