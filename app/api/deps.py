"""
app/api/deps.py
───────────────
Shared FastAPI dependencies (authentication, job ownership).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import Job, User, get_job, get_user_by_api_key, get_user_by_id
from app.core.security import API_KEY_PREFIX, decode_access_token

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: Optional[str] = Depends(_api_key_header),
) -> User:
    """Authenticate with an API key (`X-API-Key` or `Authorization: Bearer ocr_...`)
    or with a user access JWT. Returns the owning user."""
    bearer = creds.credentials if creds else None
    api_key = x_api_key or (bearer if bearer and bearer.startswith(API_KEY_PREFIX) else None)

    if api_key:
        user = await get_user_by_api_key(api_key)
        if user is None:
            raise _unauthorized("Clé API invalide ou révoquée.")
        return user

    if bearer is None:
        raise _unauthorized("Token manquant.")
    user_id = decode_access_token(bearer)
    user = await get_user_by_id(user_id) if user_id else None
    if user is None:
        raise _unauthorized("Token invalide ou expiré.")
    return user


async def get_interactive_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: Optional[str] = Depends(_api_key_header),
) -> User:
    """Login (JWT) only — API keys are refused, so a leaked key cannot mint more keys."""
    if x_api_key or (creds and creds.credentials.startswith(API_KEY_PREFIX)):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Cette action requiert une connexion utilisateur, pas une clé API.")
    return await get_current_user(creds, None)


async def get_owned_job(job_id: str, user: User = Depends(get_current_user)) -> Job:
    """Load a job and 404 unless it belongs to the current user (no existence leak)."""
    job = await get_job(job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job
