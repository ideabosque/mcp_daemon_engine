# -*- coding: utf-8 -*-
"""PostgreSQL repository for MCPModule entity.

Implements the EntityRepository contract using SQLAlchemy queries
against the PostgreSQL MCPModuleModel (table: mcp_modules).

Secondary index: idx_mcp_modules_partition_package_name

Cache cascade: after successful insert_update/delete, walks the module's
``classes`` JSON for ``setting_id`` values and purges the corresponding
``mcp_setting`` caches in addition to the ``mcp_module`` cache.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ....types.mcp_module import MCPModuleListType, MCPModuleType
from ....utils.normalization import normalize_to_json
from ...postgresql.base import normalize_row
from ...postgresql.mcp_module import MCPModuleModel
from ..base import EntityRepository


class MCPModulePGRepository(EntityRepository):
    """PostgreSQL repository for MCPModule entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_module"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        module_name = keys.get("module_name")
        if not partition_key or not module_name:
            return None
        session = Config.db_session
        row = (
            session.query(MCPModuleModel)
            .filter(
                MCPModuleModel.partition_key == partition_key,
                MCPModuleModel.module_name == module_name,
            )
            .first()
        )
        return normalize_row(row) if row else None

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        module_name = keys.get("module_name")
        if not partition_key or not module_name:
            return 0
        session = Config.db_session
        return (
            session.query(MCPModuleModel)
            .filter(
                MCPModuleModel.partition_key == partition_key,
                MCPModuleModel.module_name == module_name,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> Any:
        """Return paginated mcp_module list matching the GraphQL connection shape."""
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        page_number = filters.get("page_number", 1)
        limit = filters.get("limit", 10)
        package_name = filters.get("package_name")
        module_name = filters.get("module_name")

        query = session.query(MCPModuleModel)
        if partition_key:
            query = query.filter(MCPModuleModel.partition_key == partition_key)
        if package_name:
            query = query.filter(MCPModuleModel.package_name == package_name)
        if module_name:
            query = query.filter(
                MCPModuleModel.module_name.ilike(f"%{module_name}%")
            )

        total = query.count()
        offset = (page_number - 1) * limit
        rows = (
            query.order_by(MCPModuleModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        mcp_module_list = [self.get_type(info, row) for row in rows]
        return MCPModuleListType(mcp_module_list=mcp_module_list, total=total)

    def insert_update(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        module_name = kwargs.get("module_name")

        try:
            if module_name:
                # Update existing
                row = (
                    session.query(MCPModuleModel)
                    .filter(
                        MCPModuleModel.partition_key == partition_key,
                        MCPModuleModel.module_name == module_name,
                    )
                    .first()
                )
                if not row:
                    row = self._create_row(info, **kwargs)
                    session.add(row)
                else:
                    field_map = [
                        "package_name",
                        "classes",
                        "source",
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

            # Purge cache after successful commit — mcp_module plus
            # cascaded mcp_setting caches for any setting_ids in classes.
            classes = getattr(row, "classes", None) or kwargs.get("classes")
            self._purge_cache(info, partition_key, module_name, classes)

            return result

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()

    def _create_row(self, info: ResolveInfo, **kwargs: Any) -> MCPModuleModel:
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )

        cols: Dict[str, Any] = {
            "partition_key": partition_key,
            "module_name": kwargs["module_name"],
            "package_name": kwargs.get("package_name"),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in ["package_name", "classes", "source"]:
            if key in kwargs:
                cols[key] = kwargs[key]

        return MCPModuleModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        module_name = kwargs.get("module_name")

        try:
            row = (
                session.query(MCPModuleModel)
                .filter(
                    MCPModuleModel.partition_key == partition_key,
                    MCPModuleModel.module_name == module_name,
                )
                .first()
            )
            if not row:
                return True  # Already deleted

            # Capture classes before deletion for cascade purge
            classes = getattr(row, "classes", None)

            session.delete(row)
            session.commit()

            # Purge cache after successful commit — mcp_module plus
            # cascaded mcp_setting caches for any setting_ids in classes.
            self._purge_cache(info, partition_key, module_name, classes)

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
        module_name: str,
        classes: Any,
    ) -> None:
        """Purge cascading cache after successful insert_update or delete.

        Purges the mcp_module cache, then walks ``classes`` for
        ``setting_id`` values and purges each corresponding mcp_setting
        cache.
        """
        if not partition_key or not module_name:
            return
        try:
            from ...dynamodb.cache import (
                _extract_module_setting_ids,
                purge_entity_cascading_cache,
            )

            purge_entity_cascading_cache(
                info.context.get("logger"),
                entity_type="mcp_module",
                context_keys={"partition_key": partition_key},
                entity_keys={"module_name": module_name},
                cascade_depth=3,
            )

            # Cascade into mcp_setting caches for setting_ids in classes
            if classes:
                setting_ids = _extract_module_setting_ids(classes)
                for setting_id in setting_ids:
                    purge_entity_cascading_cache(
                        info.context.get("logger"),
                        entity_type="mcp_setting",
                        context_keys={"partition_key": partition_key},
                        entity_keys={"setting_id": setting_id},
                        cascade_depth=3,
                    )
        except Exception:
            pass

    def get_type(
        self, info: ResolveInfo, row: Any
    ) -> Optional[MCPModuleType]:
        """Convert a SQLAlchemy row to MCPModuleType."""
        data = normalize_row(row)
        if data is None:
            return None
        return MCPModuleType(**normalize_to_json(data))

    def resolve_single(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[MCPModuleType]:
        """Resolve a single mcp_module by partition_key and module_name."""
        partition_key = info.context.get("partition_key")
        module_name = kwargs.get("module_name")
        if not module_name:
            return None

        count = self.count(partition_key=partition_key, module_name=module_name)
        if count == 0:
            return None

        row = (
            Config.db_session.query(MCPModuleModel)
            .filter(
                MCPModuleModel.partition_key == partition_key,
                MCPModuleModel.module_name == module_name,
            )
            .first()
        )
        return self.get_type(info, row) if row else None


__all__ = ["MCPModulePGRepository"]