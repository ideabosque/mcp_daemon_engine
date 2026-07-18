# -*- coding: utf-8 -*-
"""Backend dispatch boundary for repository and loader selection.

``get_repo(entity_type)`` returns the active repository based on
``Config.DB_BACKEND``. ``get_loaders(context)`` is a forward-compatible
stub (no nested resolvers exist today).
"""
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Dict, Optional

from ...handlers.config import Config
from .base import EntityRepository

logger = logging.getLogger(__name__)


# --- Repository registry -----------------------------------------------------

_repo_registry: Dict[str, Dict[str, EntityRepository]] = {
    "dynamodb": {},
    "postgresql": {},
}


def register_repo(backend: str, entity_type: str, repo: EntityRepository) -> None:
    """Register a repository instance for a backend + entity_type."""
    if backend not in _repo_registry:
        raise ValueError(f"Unknown backend: {backend}")
    _repo_registry[backend][entity_type] = repo


def get_repo(entity_type: str) -> EntityRepository:
    """Return the active repository for the given entity type.

    Raises KeyError if no repository is registered for the current
    backend + entity_type combination.
    """
    backend = Config.DB_BACKEND
    repo = _repo_registry.get(backend, {}).get(entity_type)
    if repo is None:
        # Lazily initialize repos on first access
        if backend == "dynamodb":
            _init_dynamodb_repos()
            repo = _repo_registry["dynamodb"].get(entity_type)
        elif backend == "postgresql":
            _init_postgresql_repos()
            repo = _repo_registry["postgresql"].get(entity_type)

    if repo is None:
        raise KeyError(
            f"No repository registered for entity '{entity_type}' "
            f"on backend '{backend}'"
        )
    return repo


def get_loaders(context: Dict[str, Any]) -> Any:
    """Return request-scoped loaders for the active backend.

    No nested resolvers exist today, so this is a stub.
    Returns None — implement when a nested-resolver surface is added.
    """
    return None


# --- Lazy initialization -----------------------------------------------------

_dynamodb_repos_initialized = False
_postgresql_repos_initialized = False


def _init_dynamodb_repos() -> None:
    """Lazily register all DynamoDB repositories.

    The ``initialized`` flag is set only when every repo registered
    successfully. If any failed (e.g. a transient import error at startup), the
    flag stays False so the next get_repo() retries instead of leaving the
    registry permanently missing an entity.
    """
    global _dynamodb_repos_initialized
    if _dynamodb_repos_initialized:
        return

    from .dynamodb import register_all as register_dynamodb

    failures = register_dynamodb(_repo_registry["dynamodb"])
    if failures:
        logger.warning(
            "DynamoDB repo registration incomplete (%d failed); "
            "will retry on next access.",
            len(failures),
        )
    else:
        _dynamodb_repos_initialized = True


def _init_postgresql_repos() -> None:
    """Lazily register all PostgreSQL repositories.

    See _init_dynamodb_repos: only lock in initialization on full success, so a
    repo that failed to import once (e.g. mid circular-import at startup) is
    retried rather than left permanently unregistered.
    """
    global _postgresql_repos_initialized
    if _postgresql_repos_initialized:
        return

    from .postgresql import register_all as register_postgresql

    failures = register_postgresql(_repo_registry["postgresql"])
    if failures:
        logger.warning(
            "PostgreSQL repo registration incomplete (%d failed); "
            "will retry on next access.",
            len(failures),
        )
    else:
        _postgresql_repos_initialized = True


def clear_registry() -> None:
    """Clear all registered repositories (useful for tests)."""
    global _dynamodb_repos_initialized, _postgresql_repos_initialized
    _repo_registry["dynamodb"].clear()
    _repo_registry["postgresql"].clear()
    _dynamodb_repos_initialized = False
    _postgresql_repos_initialized = False