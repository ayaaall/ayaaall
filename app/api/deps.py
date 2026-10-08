"""
app/api/deps.py
───────────────
Shared FastAPI dependencies (authentication, job ownership).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import Job, User, get_job, get_user_by_id
from app.core.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    """Require a valid Bearer access token; return the authenticated user."""
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token manquant.",
                            headers={"WWW-Authenticate": "Bearer"})
    user_id = decode_access_token(creds.credentials)
    user = await get_user_by_id(user_id) if user_id else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token invalide ou expiré.",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


async def get_owned_job(job_id: str, user: User = Depends(get_current_user)) -> Job:
    """Load a job and 404 unless it belongs to the current user (no existence leak)."""
    job = await get_job(job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job
