"""
app/api/auth_routes.py
──────────────────────
Authentication endpoints: register, login, refresh, logout, me.

Auth model
──────────
- Login returns a short-lived JWT access token plus a long-lived
  refresh token persisted (hashed) in the `sessions` table (revocable).
- Clients send `Authorization: Bearer <access_token>`.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_current_user
from app.core.config import ALLOW_REGISTRATION
from app.core.database import (
    User,
    consume_session,
    create_session,
    create_user,
    get_user_by_id,
    get_user_by_identifiant,
    revoke_session,
)
from app.core.security import (
    create_access_token,
    hash_password,
    login_throttle,
    new_refresh_token,
    verify_password_constant_time,
)
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _user_response(user: User) -> UserResponse:
    return UserResponse(id=user.id, identifiant=user.identifiant, name=user.name)


async def _issue_tokens(user: User) -> TokenResponse:
    refresh = new_refresh_token()
    await create_session(refresh, user.id)
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=refresh,
        user=_user_response(user),
    )


def _client_ip(request: Request) -> str:
    # Behind nginx this is the proxy unless uvicorn runs with --proxy-headers
    # and --forwarded-allow-ips set to the proxy (see Dockerfile).
    return request.client.host if request.client else "unknown"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest):
    """Create an account: identifiant + mot de passe (+ nom optionnel)."""
    if not ALLOW_REGISTRATION:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Les inscriptions sont désactivées.")
    if await get_user_by_identifiant(body.identifiant) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Cet identifiant est déjà utilisé.")
    try:
        user = await create_user(
            user_id=f"usr_{uuid.uuid4().hex[:10]}",
            identifiant=body.identifiant,
            password_hash=hash_password(body.password),
            name=body.name,
        )
    except IntegrityError:  # concurrent registration of the same identifiant
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Cet identifiant est déjà utilisé.")
    log.info("User registered: %s", user.id)
    return await _issue_tokens(user)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    """Authenticate with identifiant + mot de passe (throttled on failures)."""
    key = f"{_client_ip(request)}|{body.identifiant}"
    if login_throttle.is_blocked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Trop de tentatives. Réessayez plus tard.",
                            headers={"Retry-After": "300"})

    user = await get_user_by_identifiant(body.identifiant)
    ok = verify_password_constant_time(body.password, user.password_hash if user else None)
    if not ok or user is None:
        login_throttle.record_failure(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail="Identifiant ou mot de passe incorrect.")
    login_throttle.reset(key)
    return await _issue_tokens(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    """Rotate a refresh token for a new access token pair (single use)."""
    sess = await consume_session(body.refresh_token)
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Session expirée.")
    user = await get_user_by_id(sess.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Utilisateur introuvable.")
    return await _issue_tokens(user)


@router.post("/logout")
async def logout(body: LogoutRequest):
    """Revoke a refresh token (logout)."""
    await revoke_session(body.refresh_token)
    return {"logged_out": True}


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)):
    """Return the current authenticated user."""
    return _user_response(user)
