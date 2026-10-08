"""
app/schemas/auth.py
───────────────────
Pydantic models for the authentication endpoints.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.core.config import PASSWORD_MIN_LENGTH
from app.core.security import MAX_PASSWORD_BYTES

_IDENTIFIANT_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._@-")


def _normalise_identifiant(value: str) -> str:
    return value.strip().lower()


class RegisterRequest(BaseModel):
    identifiant: str = Field(min_length=3, max_length=64)
    password:    str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)
    name:        Optional[str] = Field(default=None, max_length=120)

    @field_validator("identifiant")
    @classmethod
    def _check_identifiant(cls, v: str) -> str:
        v = _normalise_identifiant(v)
        if len(v) < 3 or not set(v) <= _IDENTIFIANT_ALLOWED:
            raise ValueError("Identifiant: lettres, chiffres et . _ @ - uniquement (3 caractères min).")
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"Mot de passe trop long (max {MAX_PASSWORD_BYTES} octets).")
        return v

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() or None if v else None


class LoginRequest(BaseModel):
    identifiant: str = Field(min_length=1, max_length=64)
    password:    str = Field(min_length=1, max_length=128)

    @field_validator("identifiant")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return _normalise_identifiant(v)


class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    user:          "UserResponse"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id:          str
    identifiant: str
    name:        Optional[str] = None
