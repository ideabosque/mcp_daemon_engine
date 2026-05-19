# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from time import monotonic
from typing import Any, Dict

import httpx
from fastapi import HTTPException
from jose import JWTError, jwt

from .config import Config

_JWKS_CACHE: Dict[str, Any] | None = None
_JWKS_EXPIRES_AT = 0.0
_HTTP_CLIENT: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    """Get or create the shared async HTTP client with HTTP/2 support"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=10.0,
            http2=True,  # Enable HTTP/2 support
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )
    return _HTTP_CLIENT


async def _jwks() -> Dict[str, Any]:
    """Fetch JWKS from Cognito with caching and HTTP/2 support"""
    global _JWKS_CACHE, _JWKS_EXPIRES_AT
    now = monotonic()
    if _JWKS_CACHE is None or now >= _JWKS_EXPIRES_AT:
        client = await _get_http_client()
        resp = await client.get(Config.jwks_endpoint)
        resp.raise_for_status()
        _JWKS_CACHE = resp.json()
        _JWKS_EXPIRES_AT = now + (Config.jwks_cache_ttl or 3600)
    assert _JWKS_CACHE is not None
    return _JWKS_CACHE


async def verify_cognito_jwt(token: str) -> Dict[str, Any]:
    """Verify Cognito JWT token asynchronously with HTTP/2 support"""
    try:
        head = jwt.get_unverified_header(token)
        jwks_data = await _jwks()
        key = next(k for k in jwks_data["keys"] if k["kid"] == head["kid"])
        claims = jwt.decode(
            token,
            key,
            algorithms=[key["alg"]],
            audience=Config.cognito_app_client_id,
            issuer=Config.issuer,
        )
        return claims
    except (JWTError, StopIteration) as e:
        raise HTTPException(
            status_code=401,
            detail="Invalid Cognito JWT",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def cleanup_http_client():
    """Cleanup the HTTP client on shutdown"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        await _HTTP_CLIENT.aclose()
        _HTTP_CLIENT = None
