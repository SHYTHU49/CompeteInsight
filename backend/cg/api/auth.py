"""Authentication API."""

from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request, Response, status

from cg.auth import clear_session_cookie, session_user_from_request, set_session_cookie
from cg.settings import get_settings

router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class SessionResponse(BaseModel):
    authenticated: bool
    username: str | None = None


@router.post("/login", response_model=SessionResponse)
async def login(body: LoginRequest, response: Response) -> SessionResponse:
    settings = get_settings()
    if body.username != settings.cg_auth_username or body.password != settings.cg_auth_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )
    set_session_cookie(response, settings.cg_auth_username, settings)
    return SessionResponse(authenticated=True, username=settings.cg_auth_username)


@router.post("/logout", response_model=SessionResponse)
async def logout(response: Response) -> SessionResponse:
    clear_session_cookie(response)
    return SessionResponse(authenticated=False)


@router.get("/me", response_model=SessionResponse)
async def me(request: Request) -> SessionResponse:
    username = session_user_from_request(request)
    return SessionResponse(authenticated=bool(username), username=username)

