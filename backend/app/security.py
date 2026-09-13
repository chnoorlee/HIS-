import hashlib
import hmac
import secrets
import time
from typing import NoReturn

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AppSetting, Audit, CaptureSession, Encounter, User

bearer = HTTPBearer(auto_error=False)
jwks = jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True) if settings.oidc_jwks_url else None


def fail(code: str, message: str, status: int = 400, **extra) -> NoReturn:
    raise HTTPException(status, {"code": code, "message": message, **extra})


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()


def check_password(password: str, encoded: str) -> bool:
    if "$" not in encoded:
        return False
    salt, digest = encoded.split("$", 1)
    return hmac.compare_digest(digest, hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex())


def user_json(user):
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "roles": user.roles, "hospital_id": user.hospital_id, "encounter_ids": user.encounter_ids}


def make_token(user: User):
    return jwt.encode({"sub": user.id, "ver": user.auth_version, "iat": int(time.time()), "exp": int(time.time()) + 28800, "iss": "his-local", "aud": "his-workspace"}, settings.jwt_secret, algorithm="HS256")


def decode_user(token: str, db: Session) -> User:
    try:
        if settings.env == "production":
            if jwks is None:
                fail("unauthorized", "Identity verifier is unavailable", 401)
            key = jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=settings.oidc_audience, issuer=settings.oidc_issuer, options={"require": ["sub", "exp", "iss", "aud"]})
            user = db.scalar(select(User).where(User.oidc_subject == claims["sub"]))
            if user:
                hospital = db.get(AppSetting, user.hospital_id)
                cutoff = hospital.value.get("recovery_started_at", 0) if hospital else 0
                issued_at = claims.get("iat")
                if cutoff and (not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool) or issued_at < cutoff):
                    fail("unauthorized", "Sign in again after hospital recovery", 401)
        else:
            claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], audience="his-workspace", issuer="his-local")
            user = db.get(User, claims["sub"])
            if user and user.auth_version != claims.get("ver"):
                user = None
        if not user or not user.active:
            fail("unauthorized", "Identity is unavailable or revoked", 401)
        return user
    except jwt.PyJWTError:
        fail("unauthorized", "Authentication expired or invalid", 401)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)):
    if not credentials:
        fail("unauthorized", "Authentication required", 401)
    return decode_user(credentials.credentials, db)


def require_role(user: User, *roles):
    if not set(user.roles).intersection(roles):
        fail("forbidden", "Required clinical or administrative role is missing", 403)


def access_encounter(db, user, encounter_id):
    db.refresh(user)
    encounter = db.get(Encounter, encounter_id)
    if not user.active or not encounter or encounter.hospital_id != user.hospital_id or encounter_id not in user.encounter_ids:
        fail("forbidden", "Encounter access is not authorized", 403)
    return encounter


def access_session(db, user, session_id, allow_quarantined=False, for_update=False):
    query = select(CaptureSession).where(CaptureSession.id == session_id).execution_options(populate_existing=True)
    if for_update:
        query = query.with_for_update()
    session = db.scalar(query)
    if not session:
        fail("not_found", "Session not found", 404)
    encounter = access_encounter(db, user, session.encounter_id)
    if session.hospital_id != encounter.hospital_id or session.patient_id != encounter.patient_id:
        fail("binding_mismatch", "Session identity does not match its authorized encounter", 403)
    if session.status in {"QUARANTINED", "DELETED"} and not allow_quarantined:
        fail("quarantined", "Session materials are quarantined or deleted", 423)
    return session


def audit(db, user, action, resource_id, **detail):
    db.add(Audit(hospital_id=user.hospital_id, actor_id=user.id, action=action, resource_id=resource_id, detail=detail))
