# -*- coding: utf-8 -*-
"""PostgreSQL repositories for the PostgreSQL backend.

All PG repository files live under models/repositories/postgresql/.
Import paths are clean:
  from ...postgresql.base import normalize_row       # models/postgresql/base.py
  from ...postgresql.mcp_function import MCPFunctionModel  # models/postgresql/mcp_function.py
  from ..base import EntityRepository  # models/repositories/base.py
  from ....handlers.config import Config   # mcp_daemon_engine/handlers/config.py
  from ....types.mcp_function import MCPFunctionType  # mcp_daemon_engine/types/mcp_function.py
"""
from __future__ import print_function

__author__ = "bibow"

import importlib
import logging
from typing import Dict, List, Tuple

from ..base import EntityRepository

logger = logging.getLogger(__name__)


def register_all(registry: Dict[str, EntityRepository]) -> List[Tuple[str, str, Exception]]:
    """Register all PostgreSQL repositories into the given registry dict.

    Returns a list of ``(module_name, class_name, error)`` for repos that could
    not be registered. Failures are logged (not silently swallowed) and never
    abort the loop, so one bad repo cannot block the others. The dispatch layer
    uses the return value to decide whether to retry on the next access — a repo
    can fail transiently (e.g. mid circular-import at startup) and succeed later.
    """
    _repos = [
        ("mcp_function_repo", "MCPFunctionPGRepository"),
        ("mcp_module_repo", "MCPModulePGRepository"),
        ("mcp_setting_repo", "MCPSettingPGRepository"),
        ("mcp_function_call_repo", "MCPFunctionCallPGRepository"),
    ]
    failures: List[Tuple[str, str, Exception]] = []
    for module_name, class_name in _repos:
        try:
            mod = importlib.import_module(f".{module_name}", package=__name__)
            repo_cls = getattr(mod, class_name)
            repo = repo_cls()
            registry[repo.entity_type] = repo
        except Exception as e:  # noqa: BLE001 - report every failure, keep going
            logger.warning(
                "Failed to register PostgreSQL repo %s.%s: %s: %s",
                module_name,
                class_name,
                type(e).__name__,
                e,
            )
            failures.append((module_name, class_name, e))
    return failures


__all__ = ["register_all"]