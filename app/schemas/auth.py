"""
app/schemas/auth.py
───────────────────
Pydantic models for the authentication endpoints.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    identifiant: str = Field(min_length=3, max_length=64)
    password:    str = Field(min_length=6, max_length=128)
    name:        Optional[str] = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    identifiant: str
    password:    str


class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    user:          "UserResponse"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id:          str
    identifiant: str
    name:        Optional[str] = None
