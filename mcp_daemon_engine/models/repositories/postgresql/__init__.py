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

from typing import Dict

from ..base import EntityRepository


def register_all(registry: Dict[str, EntityRepository]) -> None:
    """Register all PostgreSQL repositories into the given registry dict."""
    _repos = [
        ("mcp_function_repo", "MCPFunctionPGRepository"),
        ("mcp_module_repo", "MCPModulePGRepository"),
        ("mcp_setting_repo", "MCPSettingPGRepository"),
        ("mcp_function_call_repo", "MCPFunctionCallPGRepository"),
    ]
    for module_name, class_name in _repos:
        try:
            import importlib

            mod = importlib.import_module(f".{module_name}", package=__name__)
            repo_cls = getattr(mod, class_name)
            repo = repo_cls()
            registry[repo.entity_type] = repo
        except ImportError:
            pass


__all__ = ["register_all"]