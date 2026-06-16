#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

__all__ = [
    "AuthenticationError",
    "InvalidRequestError",
    "MCPDaemonEngine",
    "RateLimitExceeded",
    "deploy",
]
from .utils.exceptions import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitExceeded,
)


def __getattr__(name):
    """
    Lazily import runtime entrypoints so metadata helpers remain available
    without reintroducing the old CLI main export.
    """
    if name in {"MCPDaemonEngine", "deploy"}:
        from .main import MCPDaemonEngine, deploy

        exports = {
            "MCPDaemonEngine": MCPDaemonEngine,
            "deploy": deploy,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
