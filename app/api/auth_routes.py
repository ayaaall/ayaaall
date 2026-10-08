"""
app/api/auth_routes.py
──────────────────────
Authentication endpoints: register, login, refresh, logout, me.

Auth model
──────────
- Login returns a short-lived JWT access token plus a long-lived
  refresh token persisted in the `sessions` table (revocable).
- Clients send `Authorization: Bearer <access_token>`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import (
    User,
    create_session,
    create_user,
    get_user_by_id,
    get_user_by_identifiant,
    get_valid_session,
    revoke_session,
)
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
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

_bearer = HTTPBearer(auto_error=False)


# ── Dependencies ───────────────────────────────────────────────────────────────

async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    """Require a valid Bearer access token; return the authenticated user."""
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token manquant.")
    user_id = decode_access_token(creds.credentials)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token invalide ou expiré.")
    user = await get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Utilisateur introuvable.")
    return user


def _user_response(user: User) -> UserResponse:
    return UserResponse(id=user.id, identifiant=user.identifiant, name=user.name)


async def _issue_tokens(user: User) -> TokenResponse:
    refresh = uuid.uuid4().hex
    await create_session(refresh, user.id)
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=refresh,
        user=_user_response(user),
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest):
    """Create an account: identifiant + mot de passe (+ nom optionnel)."""
    existing = await get_user_by_identifiant(body.identifiant)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Cet identifiant est déjà utilisé.")
    user = await create_user(
        user_id=f"usr_{uuid.uuid4().hex[:10]}",
        identifiant=body.identifiant,
        password_hash=hash_password(body.password),
        name=body.name or None,
    )
    log.info("User registered: %s", user.identifiant)
    return await _issue_tokens(user)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """Authenticate with identifiant + mot de passe."""
    user = await get_user_by_identifiant(body.identifiant)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail="Identifiant ou mot de passe incorrect.")
    return await _issue_tokens(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    """Rotate a refresh token for a new access token pair."""
    sess = await get_valid_session(body.refresh_token)
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Session expirée.")
    await revoke_session(body.refresh_token)  # rotate: old token dies
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
