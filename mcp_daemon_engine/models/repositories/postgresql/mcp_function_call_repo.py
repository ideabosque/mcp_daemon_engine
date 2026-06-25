# -*- coding: utf-8 -*-
"""PostgreSQL repository for MCPFunctionCall entity.

Implements the EntityRepository contract using SQLAlchemy queries
against the PostgreSQL MCPFunctionCallModel (table: mcp_function_calls).

S3 content offload divergence from DynamoDB:
- DynamoDB auto-offloads content to S3 when the item exceeds 400KB.
- PostgreSQL has no such row size limit, so the PG repository only
  offloads to S3 when the caller explicitly sets ``content_in_s3=True``.
- The PG repository still hydrates content from S3 when
  ``content_in_s3`` is set on an existing row (get/list/get_type).

Secondary indexes:
  idx_mcp_function_calls_partition_mcp_type
  idx_mcp_function_calls_partition_name
  idx_mcp_function_calls_partition_updated_at

List ordering: newest-first (order_by updated_at.desc()).
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
import uuid
from typing import Any, Dict, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ....types.mcp_function_call import (
    MCPFunctionCallListType,
    MCPFunctionCallType,
)
from ....utils.normalization import normalize_to_json
from ...postgresql.base import normalize_row
from ...postgresql.mcp_function_call import MCPFunctionCallModel
from ..base import EntityRepository


class MCPFunctionCallPGRepository(EntityRepository):
    """PostgreSQL repository for MCPFunctionCall entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_function_call"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        mcp_function_call_uuid = keys.get("mcp_function_call_uuid")
        if not partition_key or not mcp_function_call_uuid:
            return None
        session = Config.db_session
        row = (
            session.query(MCPFunctionCallModel)
            .filter(
                MCPFunctionCallModel.partition_key == partition_key,
                MCPFunctionCallModel.mcp_function_call_uuid
                == mcp_function_call_uuid,
            )
            .first()
        )
        if not row:
            return None
        data = normalize_row(row)
        # S3 hydration
        if row.content_in_s3:
            data["content"] = self._fetch_content_from_s3(
                mcp_function_call_uuid,
                info_logger=None,
            )
        return data

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        mcp_function_call_uuid = keys.get("mcp_function_call_uuid")
        if not partition_key or not mcp_function_call_uuid:
            return 0
        session = Config.db_session
        return (
            session.query(MCPFunctionCallModel)
            .filter(
                MCPFunctionCallModel.partition_key == partition_key,
                MCPFunctionCallModel.mcp_function_call_uuid
                == mcp_function_call_uuid,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> Any:
        """Return paginated mcp_function_call list matching the GraphQL
        connection shape, ordered newest-first."""
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        page_number = filters.get("page_number", 1)
        limit = filters.get("limit", 10)
        mcp_type = filters.get("mcp_type")
        name = filters.get("name")
        status = filters.get("status")
        updated_at_gt = filters.get("updated_at_gt")
        updated_at_lt = filters.get("updated_at_lt")

        query = session.query(MCPFunctionCallModel)
        if partition_key:
            query = query.filter(
                MCPFunctionCallModel.partition_key == partition_key
            )
        if mcp_type:
            query = query.filter(MCPFunctionCallModel.mcp_type == mcp_type)
        if name:
            query = query.filter(MCPFunctionCallModel.name == name)
        if status:
            query = query.filter(MCPFunctionCallModel.status == status)
        if updated_at_gt is not None:
            query = query.filter(
                MCPFunctionCallModel.updated_at > updated_at_gt
            )
        if updated_at_lt is not None:
            query = query.filter(
                MCPFunctionCallModel.updated_at < updated_at_lt
            )

        total = query.count()
        offset = (page_number - 1) * limit
        rows = (
            query.order_by(MCPFunctionCallModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        mcp_function_call_list = [self.get_type(info, row) for row in rows]
        return MCPFunctionCallListType(
            mcp_function_call_list=mcp_function_call_list, total=total
        )

    def insert_update(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        mcp_function_call_uuid = kwargs.get("mcp_function_call_uuid")

        # Auto-generate uuid if not provided
        if not mcp_function_call_uuid:
            mcp_function_call_uuid = str(uuid.uuid4())
            kwargs["mcp_function_call_uuid"] = mcp_function_call_uuid

        try:
            # S3 offload — only when caller explicitly sets
            # content_in_s3=True (no auto-offload on size).
            offload_to_s3 = bool(kwargs.get("content_in_s3"))
            db_kwargs = dict(kwargs)
            if offload_to_s3 and kwargs.get("content"):
                s3_key = f"mcp_content/{mcp_function_call_uuid}.json"
                self._save_content_to_s3(
                    kwargs["content"], s3_key, logger
                )
                # Don't store content in the DB row
                db_kwargs.pop("content", None)
                db_kwargs["content_in_s3"] = True
            else:
                # If not offloading, content_in_s3 should be False
                db_kwargs["content_in_s3"] = bool(
                    db_kwargs.get("content_in_s3")
                )

            # Update existing
            row = (
                session.query(MCPFunctionCallModel)
                .filter(
                    MCPFunctionCallModel.partition_key == partition_key,
                    MCPFunctionCallModel.mcp_function_call_uuid
                    == mcp_function_call_uuid,
                )
                .first()
            )
            if row:
                field_map = [
                    "name",
                    "mcp_type",
                    "arguments",
                    "content",
                    "content_in_s3",
                    "status",
                    "notes",
                    "time_spent",
                ]
                for field in field_map:
                    if field in db_kwargs:
                        val = db_kwargs[field]
                        setattr(
                            row,
                            field,
                            None if val == "null" else val,
                        )
                row.updated_by = db_kwargs["updated_by"]
                row.updated_at = pendulum.now("UTC")
            else:
                # Create new
                row = self._create_row(info, **db_kwargs)
                session.add(row)

            session.commit()
            session.refresh(row)
            result = normalize_row(row)

            # Purge cache after successful commit
            self._purge_cache(info, partition_key, mcp_function_call_uuid)

            return result

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()

    def _create_row(
        self, info: ResolveInfo, **kwargs: Any
    ) -> MCPFunctionCallModel:
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )

        cols: Dict[str, Any] = {
            "partition_key": partition_key,
            "mcp_function_call_uuid": kwargs["mcp_function_call_uuid"],
            "name": kwargs.get("name"),
            "mcp_type": kwargs.get("mcp_type"),
            "status": kwargs.get("status", "initial"),
            "content_in_s3": bool(kwargs.get("content_in_s3")),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "name",
            "mcp_type",
            "arguments",
            "content",
            "content_in_s3",
            "status",
            "notes",
            "time_spent",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        return MCPFunctionCallModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        mcp_function_call_uuid = kwargs.get("mcp_function_call_uuid")

        try:
            row = (
                session.query(MCPFunctionCallModel)
                .filter(
                    MCPFunctionCallModel.partition_key == partition_key,
                    MCPFunctionCallModel.mcp_function_call_uuid
                    == mcp_function_call_uuid,
                )
                .first()
            )
            if not row:
                return True  # Already deleted

            session.delete(row)
            session.commit()

            # Purge cache after successful commit
            self._purge_cache(info, partition_key, mcp_function_call_uuid)

            return True

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()

    def _purge_cache(
        self,
        info: ResolveInfo,
        partition_key: str,
        mcp_function_call_uuid: str,
    ) -> None:
        """Purge cascading cache after successful insert_update or delete."""
        if not partition_key or not mcp_function_call_uuid:
            return
        try:
            from ...dynamodb.cache import purge_entity_cascading_cache

            purge_entity_cascading_cache(
                info.context.get("logger"),
                entity_type="mcp_function_call",
                context_keys={"partition_key": partition_key},
                entity_keys={
                    "mcp_function_call_uuid": mcp_function_call_uuid
                },
                cascade_depth=3,
            )
        except Exception:
            pass

    def get_type(
        self, info: ResolveInfo, row: Any
    ) -> Optional[MCPFunctionCallType]:
        """Convert a SQLAlchemy row to MCPFunctionCallType.

        Hydrates ``content`` from S3 when ``content_in_s3`` is True on the
        row.
        """
        data = normalize_row(row)
        if data is None:
            return None

        # S3 hydration
        if getattr(row, "content_in_s3", False):
            uuid_val = getattr(row, "mcp_function_call_uuid", None)
            if uuid_val:
                data["content"] = self._fetch_content_from_s3(
                    uuid_val,
                    info_logger=info.context.get("logger"),
                )

        return MCPFunctionCallType(**normalize_to_json(data))

    def resolve_single(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[MCPFunctionCallType]:
        """Resolve a single mcp_function_call by partition_key and uuid."""
        partition_key = info.context.get("partition_key")
        mcp_function_call_uuid = kwargs.get("mcp_function_call_uuid")
        if not mcp_function_call_uuid:
            return None

        count = self.count(
            partition_key=partition_key,
            mcp_function_call_uuid=mcp_function_call_uuid,
        )
        if count == 0:
            return None

        row = (
            Config.db_session.query(MCPFunctionCallModel)
            .filter(
                MCPFunctionCallModel.partition_key == partition_key,
                MCPFunctionCallModel.mcp_function_call_uuid
                == mcp_function_call_uuid,
            )
            .first()
        )
        return self.get_type(info, row) if row else None

    # ------------------------------------------------------------------
    # S3 helpers
    # ------------------------------------------------------------------

    def _fetch_content_from_s3(
        self, mcp_function_call_uuid: str, info_logger: Any = None
    ) -> str:
        """Fetch content from S3 (s3://bucket/mcp_content/{uuid}.json)."""
        s3_key = f"mcp_content/{mcp_function_call_uuid}.json"
        response = Config.aws_s3.get_object(
            Bucket=Config.funct_bucket_name, Key=s3_key
        )
        content = response["Body"].read().decode("utf-8")
        return content

    def _save_content_to_s3(
        self, content: str, s3_key: str, logger: Any = None
    ) -> None:
        """Save content to S3 bucket at the given key."""
        try:
            Config.aws_s3.put_object(
                Bucket=Config.funct_bucket_name, Key=s3_key, Body=content
            )
            if logger:
                logger.info(
                    f"Content saved to S3: "
                    f"s3://{Config.funct_bucket_name}/{s3_key}"
                )
        except Exception as e:
            if logger:
                logger.error(f"Failed to save content to S3: {e}")
            raise


__all__ = ["MCPFunctionCallPGRepository"]