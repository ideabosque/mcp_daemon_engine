# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import pendulum
from functools import lru_cache
from typing import Any, Dict

from fastapi import HTTPException
from jose import JWTError, jwt

from .config import Config


def _expiry():
    return pendulum.now('UTC').add(minutes=Config.access_token_exp)


def create_local_jwt(payload: Dict[str, Any], forever: bool = False) -> str:
    data = payload.copy()
    if forever:
        data["perm"] = True
    else:
        data["exp"] = _expiry()
    return jwt.encode(data, Config.jwt_secret_key, algorithm=Config.jwt_algorithm)


def verify_local_jwt(token: str) -> Dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            Config.jwt_secret_key,
            algorithms=[Config.jwt_algorithm],
            options={"verify_exp": False},
        )
        if not claims.get("perm"):
            if (
                claims.get("exp") is None
                or pendulum.now('UTC').timestamp() > claims["exp"]
            ):
                raise JWTError("expired")
        return claims
    except JWTError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid JWT ({e})",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


# ── static admin token helper ────────────────────────────────────
@lru_cache
def get_or_create_admin_token() -> str:
    if Config.admin_static_token:
        return Config.admin_static_token
    token = create_local_jwt(
        {"username": Config.admin_username, "role": "admin"}, forever=True
    )
    Config.logger.info(f"🔑  Generated static admin token:\n   {token}")
    return token
