# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import base64
import hashlib
import hmac
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from .config import Config, LocalUser
from .jwt_local import create_local_jwt, get_or_create_admin_token

router = APIRouter(prefix="/auth", tags=["auth"])


def authenticate(username: str, password: str) -> LocalUser | None:
    user = Config._USERS.get(username)
    return user if user and user.verify(password) else None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/token", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends()):
    if Config.auth_provider == "cognito":
        return get_cognito_token(form.username, form.password)
    else:
        return get_local_token(form.username, form.password)


def get_local_token(username: str, password: str) -> Dict[str, Any]:
    if (
        Config.admin_username
        and Config.admin_password
        and username == Config.admin_username
        and password == Config.admin_password
    ):
        return {"access_token": get_or_create_admin_token(), "token_type": "bearer"}

    # user file
    user = authenticate(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_local_jwt({"username": user.username, "roles": user.roles})
    return {"access_token": token, "token_type": "bearer"}


def get_cognito_token(username: str, password: str) -> Dict[str, Any]:
    resp = Config.aws_cognito_idp.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=Config.cognito_app_client_id,
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
            "SECRET_HASH": secret_hash(username),
        },
    )

    tokens = resp["AuthenticationResult"]

    return {"access_token": tokens["AccessToken"], "token_type": "bearer"}


def secret_hash(username: str) -> str:
    """
    Cognito expects:  Base64( HMAC-SHA256( key=client_secret, msg=username+client_id ) )
    """
    if not Config.cognito_app_client_id or not Config.cognito_app_secret:
        raise ValueError("Cognito app client ID and secret must be configured")
    message = (username + Config.cognito_app_client_id).encode("utf-8")
    key = Config.cognito_app_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()
