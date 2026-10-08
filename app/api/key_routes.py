"""
app/api/key_routes.py
─────────────────────
Self-service API-key management. Requires a user login (JWT): an API key
cannot create or list keys.

The raw key is returned ONCE at creation; only its SHA-256 digest is stored.
"""

from __future__ import annotations

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_interactive_user
from app.core.database import User, count_api_keys, create_api_key, list_api_keys, revoke_api_key
from app.core.security import new_api_key

log = logging.getLogger(__name__)
router = APIRouter(prefix="/keys", tags=["api-keys"])

MAX_KEYS_PER_USER = 10


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="Label, e.g. the consuming project")


class KeyInfo(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: str
    last_used_at: str | None = None


class CreatedKey(KeyInfo):
    api_key: str = Field(description="Shown only once — store it securely.")


@router.post("", response_model=CreatedKey, status_code=201)
async def create_key(body: CreateKeyRequest, user: User = Depends(get_interactive_user)):
    if await count_api_keys(user.id) >= MAX_KEYS_PER_USER:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"Limite de {MAX_KEYS_PER_USER} clés atteinte. Révoquez-en une.")
    raw = new_api_key()
    row = await create_api_key(f"key_{uuid.uuid4().hex[:12]}", user.id, body.name.strip(), raw)
    log.info("API key %s created for user %s", row.id, user.id)
    return CreatedKey(id=row.id, name=row.name, prefix=row.prefix,
                      created_at=row.created_at, api_key=raw)


@router.get("", response_model=List[KeyInfo])
async def list_keys(user: User = Depends(get_interactive_user)):
    return [KeyInfo(id=k.id, name=k.name, prefix=k.prefix, created_at=k.created_at,
                    last_used_at=k.last_used_at) for k in await list_api_keys(user.id)]


@router.delete("/{key_id}")
async def delete_key(key_id: str, user: User = Depends(get_interactive_user)):
    if not await revoke_api_key(key_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Clé introuvable.")
    log.info("API key %s revoked by user %s", key_id, user.id)
    return {"revoked": True, "id": key_id}
