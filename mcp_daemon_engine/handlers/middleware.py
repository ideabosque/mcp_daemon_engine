# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"
from typing import Iterable, List

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import Config
from .jwt_cognito import verify_cognito_jwt
from .jwt_local import verify_local_jwt


class FlexJWTMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, public_paths: Iterable[str] = ()):
        super().__init__(app)
        self.public_paths: List[str] = list(public_paths) + ["/auth"]

    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(p) for p in self.public_paths):
            return await call_next(request)

        auth = request.headers.get("authorization")
        if not (auth and auth.lower().startswith("bearer ")):
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated"}
            )

        token = auth.split(" ", 1)[1]
        mode = Config.auth_provider

        try:
            if mode == "cognito":
                claims = await verify_cognito_jwt(token)
            else:
                claims = verify_local_jwt(token)
            request.state.user = claims
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
                headers=e.headers,
            )

        return await call_next(request)
