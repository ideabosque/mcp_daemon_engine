# -*- coding: utf-8 -*-
"""PostgreSQL repository for MCPFunction entity.

Implements the EntityRepository contract using SQLAlchemy queries
against the PostgreSQL MCPFunctionModel (table: mcp_functions).

Secondary index: idx_mcp_functions_partition_mcp_type
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ....types.mcp_function import MCPFunctionListType, MCPFunctionType
from ....utils.normalization import normalize_to_json
from ...postgresql.base import normalize_row
from ...postgresql.mcp_function import MCPFunctionModel
from ..base import EntityRepository


class MCPFunctionPGRepository(EntityRepository):
    """PostgreSQL repository for MCPFunction entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_function"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        name = keys.get("name")
        if not partition_key or not name:
            return None
        session = Config.db_session
        row = (
            session.query(MCPFunctionModel)
            .filter(
                MCPFunctionModel.partition_key == partition_key,
                MCPFunctionModel.name == name,
            )
            .first()
        )
        return normalize_row(row) if row else None

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        name = keys.get("name")
        if not partition_key or not name:
            return 0
        session = Config.db_session
        return (
            session.query(MCPFunctionModel)
            .filter(
                MCPFunctionModel.partition_key == partition_key,
                MCPFunctionModel.name == name,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> Any:
        """Return paginated mcp_function list matching the GraphQL connection shape."""
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        page_number = filters.get("page_number", 1)
        limit = filters.get("limit", 10)
        mcp_type = filters.get("mcp_type")
        description = filters.get("description")
        module_name = filters.get("module_name")
        class_name = filters.get("class_name")
        function_name = filters.get("function_name")
        enabled = filters.get("enabled")

        query = session.query(MCPFunctionModel)
        if partition_key:
            query = query.filter(MCPFunctionModel.partition_key == partition_key)
        if mcp_type:
            query = query.filter(MCPFunctionModel.mcp_type == mcp_type)
        if description:
            query = query.filter(
                MCPFunctionModel.description.ilike(f"%{description}%")
            )
        if module_name:
            query = query.filter(MCPFunctionModel.module_name == module_name)
        if class_name:
            query = query.filter(MCPFunctionModel.class_name == class_name)
        if function_name:
            query = query.filter(MCPFunctionModel.function_name == function_name)
        if enabled is not None:
            if enabled:
                # Treat NULL as True (default-enabled) — match True or NULL
                from sqlalchemy import or_
                query = query.filter(
                    or_(MCPFunctionModel.enabled == True, MCPFunctionModel.enabled.is_(None))
                )
            else:
                query = query.filter(MCPFunctionModel.enabled == False)

        total = query.count()
        offset = (page_number - 1) * limit
        rows = (
            query.order_by(MCPFunctionModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        mcp_function_list = [self.get_type(info, row) for row in rows]
        return MCPFunctionListType(
            mcp_function_list=mcp_function_list, total=total
        )

    def insert_update(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        name = kwargs.get("name")

        try:
            if name:
                # Update existing
                row = (
                    session.query(MCPFunctionModel)
                    .filter(
                        MCPFunctionModel.partition_key == partition_key,
                        MCPFunctionModel.name == name,
                    )
                    .first()
                )
                if not row:
                    row = self._create_row(info, **kwargs)
                    session.add(row)
                else:
                    field_map = [
                        "mcp_type",
                        "description",
                        "data",
                        "annotations",
                        "module_name",
                        "class_name",
                        "function_name",
                        "return_type",
                        "is_async",
                        "enabled",
                    ]
                    for field in field_map:
                        if field in kwargs:
                            val = kwargs[field]
                            setattr(
                                row,
                                field,
                                None if val == "null" else val,
                            )
                    row.updated_by = kwargs["updated_by"]
                    row.updated_at = pendulum.now("UTC")
            else:
                # Create new
                row = self._create_row(info, **kwargs)
                session.add(row)

            session.commit()
            session.refresh(row)
            result = normalize_row(row)

            # Purge cache after successful commit
            self._purge_cache(info, partition_key, name)

            return result

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e

    def _create_row(self, info: ResolveInfo, **kwargs: Any) -> MCPFunctionModel:
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )

        cols: Dict[str, Any] = {
            "partition_key": partition_key,
            "name": kwargs["name"],
            "mcp_type": kwargs.get("mcp_type"),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "mcp_type",
            "description",
            "data",
            "annotations",
            "module_name",
            "class_name",
            "function_name",
            "return_type",
            "is_async",
            "enabled",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        return MCPFunctionModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        name = kwargs.get("name")

        try:
            row = (
                session.query(MCPFunctionModel)
                .filter(
                    MCPFunctionModel.partition_key == partition_key,
                    MCPFunctionModel.name == name,
                )
                .first()
            )
            if not row:
                return True  # Already deleted

            session.delete(row)
            session.commit()

            # Purge cache after successful commit
            self._purge_cache(info, partition_key, name)

            return True

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e

    def _purge_cache(
        self, info: ResolveInfo, partition_key: str, name: str
    ) -> None:
        """Purge cascading cache after successful insert_update or delete."""
        if not partition_key or not name:
            return
        try:
            from ...dynamodb.cache import purge_entity_cascading_cache

            purge_entity_cascading_cache(
                info.context.get("logger"),
                entity_type="mcp_function",
                context_keys={"partition_key": partition_key},
                entity_keys={"name": name},
                cascade_depth=3,
            )
        except Exception:
            pass

    def get_type(
        self, info: ResolveInfo, row: Any
    ) -> Optional[MCPFunctionType]:
        """Convert a SQLAlchemy row to MCPFunctionType."""
        data = normalize_row(row)
        if data is None:
            return None
        return MCPFunctionType(**normalize_to_json(data))

    def resolve_single(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[MCPFunctionType]:
        """Resolve a single mcp_function by partition_key and name."""
        partition_key = info.context.get("partition_key")
        name = kwargs.get("name")
        if not name:
            return None

        count = self.count(partition_key=partition_key, name=name)
        if count == 0:
            return None

        row = (
            Config.db_session.query(MCPFunctionModel)
            .filter(
                MCPFunctionModel.partition_key == partition_key,
                MCPFunctionModel.name == name,
            )
            .first()
        )
        return self.get_type(info, row) if row else None


__all__ = ["MCPFunctionPGRepository"]