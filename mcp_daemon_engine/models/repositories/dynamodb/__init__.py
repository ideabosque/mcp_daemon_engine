# -*- coding: utf-8 -*-
"""DynamoDB repositories — thin wrappers over existing PynamoDB model functions.

Each entity has its own repo file. The register_all function instantiates
all 4 repositories and registers them with the dispatch registry.
"""
from __future__ import print_function

__author__ = "bibow"

import importlib
import logging
from typing import Dict, List, Tuple

from ..base import EntityRepository

logger = logging.getLogger(__name__)


def register_all(registry: Dict[str, EntityRepository]) -> List[Tuple[str, str, Exception]]:
    """Register all DynamoDB repositories into the given registry dict.

    Returns a list of ``(module_name, class_name, error)`` for repos that could
    not be registered. Failures are logged and never abort the loop, so one bad
    repo cannot block the others; the dispatch layer retries on next access.
    """
    _repos = [
        ("mcp_function_repo", "MCPFunctionRepository"),
        ("mcp_module_repo", "MCPModuleRepository"),
        ("mcp_setting_repo", "MCPSettingRepository"),
        ("mcp_function_call_repo", "MCPFunctionCallRepository"),
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
                "Failed to register DynamoDB repo %s.%s: %s: %s",
                module_name,
                class_name,
                type(e).__name__,
                e,
            )
            failures.append((module_name, class_name, e))
    return failures


__all__ = ["register_all"]