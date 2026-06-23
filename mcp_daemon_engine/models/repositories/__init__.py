# -*- coding: utf-8 -*-
"""Repository abstraction boundary for dual-backend persistence."""
from __future__ import print_function

__author__ = "bibow"

from .base import (
    DependencyExistsError,
    EntityNotFoundError,
    EntityRepository,
    RepositoryError,
)
from .dispatch import (
    clear_registry,
    get_loaders,
    get_repo,
    register_repo,
)

__all__ = [
    "EntityRepository",
    "RepositoryError",
    "EntityNotFoundError",
    "DependencyExistsError",
    "get_repo",
    "get_loaders",
    "register_repo",
    "clear_registry",
]