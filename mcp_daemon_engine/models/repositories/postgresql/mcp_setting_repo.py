# -*- coding: utf-8 -*-
"""PostgreSQL repository for MCPSetting entity.

Implements the EntityRepository contract using SQLAlchemy queries
against the PostgreSQL MCPSettingModel (table: mcp_settings).

No secondary index — queries filter on the composite primary key
(partition_key, setting_id) only.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ....types.mcp_setting import MCPSettingListType, MCPSettingType
from ....utils.normalization import normalize_to_json
from ...postgresql.base import normalize_row
from ...postgresql.mcp_setting import MCPSettingModel
from ..base import EntityRepository


class MCPSettingPGRepository(EntityRepository):
    """PostgreSQL repository for MCPSetting entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_setting"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        setting_id = keys.get("setting_id")
        if not partition_key or not setting_id:
            return None
        session = Config.db_session
        row = (
            session.query(MCPSettingModel)
            .filter(
                MCPSettingModel.partition_key == partition_key,
                MCPSettingModel.setting_id == setting_id,
            )
            .first()
        )
        return normalize_row(row) if row else None

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        setting_id = keys.get("setting_id")
        if not partition_key or not setting_id:
            return 0
        session = Config.db_session
        return (
            session.query(MCPSettingModel)
            .filter(
                MCPSettingModel.partition_key == partition_key,
                MCPSettingModel.setting_id == setting_id,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> Any:
        """Return paginated mcp_setting list matching the GraphQL connection shape."""
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        page_number = filters.get("page_number", 1)
        limit = filters.get("limit", 10)
        setting_id = filters.get("setting_id")

        query = session.query(MCPSettingModel)
        if partition_key:
            query = query.filter(MCPSettingModel.partition_key == partition_key)
        if setting_id:
            query = query.filter(
                MCPSettingModel.setting_id.ilike(f"%{setting_id}%")
            )

        total = query.count()
        offset = (page_number - 1) * limit
        rows = (
            query.order_by(MCPSettingModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        mcp_setting_list = [self.get_type(info, row) for row in rows]
        return MCPSettingListType(
            mcp_setting_list=mcp_setting_list, total=total
        )

    def insert_update(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        setting_id = kwargs.get("setting_id")

        # Auto-generate setting_id when not supplied, mirroring the DynamoDB
        # insert_update_decorator (range_key_required=False) so callers like
        # load_mcp_configuration_into_models can create a shared setting without
        # specifying an id. Same 20-digit format for cross-backend consistency.
        if not setting_id:
            import uuid

            setting_id = f"{uuid.uuid1().int % (10**20):020d}"
            kwargs["setting_id"] = setting_id

        try:
            if setting_id:
                # Update existing
                row = (
                    session.query(MCPSettingModel)
                    .filter(
                        MCPSettingModel.partition_key == partition_key,
                        MCPSettingModel.setting_id == setting_id,
                    )
                    .first()
                )
                if not row:
                    row = self._create_row(info, **kwargs)
                    session.add(row)
                else:
                    field_map = [
                        "setting",
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
            self._purge_cache(info, partition_key, setting_id)

            return result

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()

    def _create_row(self, info: ResolveInfo, **kwargs: Any) -> MCPSettingModel:
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )

        cols: Dict[str, Any] = {
            "partition_key": partition_key,
            "setting_id": kwargs["setting_id"],
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in ["setting"]:
            if key in kwargs:
                cols[key] = kwargs[key]

        return MCPSettingModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get(
            "partition_key"
        )
        setting_id = kwargs.get("setting_id")

        try:
            row = (
                session.query(MCPSettingModel)
                .filter(
                    MCPSettingModel.partition_key == partition_key,
                    MCPSettingModel.setting_id == setting_id,
                )
                .first()
            )
            if not row:
                return True  # Already deleted

            session.delete(row)
            session.commit()

            # Purge cache after successful commit
            self._purge_cache(info, partition_key, setting_id)

            return True

        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()

    def _purge_cache(
        self, info: ResolveInfo, partition_key: str, setting_id: str
    ) -> None:
        """Purge cascading cache after successful insert_update or delete."""
        if not partition_key or not setting_id:
            return
        try:
            from ...dynamodb.cache import purge_entity_cascading_cache

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
    ) -> Optional[MCPSettingType]:
        """Convert a SQLAlchemy row to MCPSettingType."""
        data = normalize_row(row)
        if data is None:
            return None
        return MCPSettingType(**normalize_to_json(data))

    def resolve_single(
        self, info: ResolveInfo, **kwargs: Any
    ) -> Optional[MCPSettingType]:
        """Resolve a single mcp_setting by partition_key and setting_id."""
        partition_key = info.context.get("partition_key")
        setting_id = kwargs.get("setting_id")
        if not setting_id:
            return None

        count = self.count(partition_key=partition_key, setting_id=setting_id)
        if count == 0:
            return None

        row = (
            Config.db_session.query(MCPSettingModel)
            .filter(
                MCPSettingModel.partition_key == partition_key,
                MCPSettingModel.setting_id == setting_id,
            )
            .first()
        )
        return self.get_type(info, row) if row else None


__all__ = ["MCPSettingPGRepository"]