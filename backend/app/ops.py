"""Offline provisioning and recovery commands for authorized hospital operators."""

import argparse
import json
import os
import secrets
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update

from . import clinical, lifecycle
from .config import settings
from .db import SessionLocal, create_schema
from .models import AppSetting, Audit, CaptureSession, Encounter, Export, Job, Note, Review, User, now


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncounterInput(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    patient_id: str = Field(min_length=1, max_length=64)
    admission_id: str = Field(min_length=1, max_length=64)
    patient_name: str = Field(min_length=1, max_length=128)
    bed: str = Field(max_length=32)
    department: str = Field(min_length=1, max_length=128)
    age: int = Field(ge=0, le=150)
    sex: str = Field(max_length=32)
    admitted_at: str = Field(min_length=1, max_length=40)
    diagnosis: str = Field(default="", max_length=10000)


class IdentityInput(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    oidc_subject: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=128)
    roles: list[Literal["doctor", "reviewer", "admin"]] = Field(min_length=1, max_length=3)
    encounter_ids: list[str] = Field(default_factory=list)
    active: bool = False


class ProvisionInput(StrictModel):
    hospital_id: str = Field(min_length=1, max_length=64)
    oidc_issuer: str
    encounters: list[EncounterInput] = Field(default_factory=list, max_length=10000)
    users: list[IdentityInput] = Field(default_factory=list, max_length=10000)


class Tombstone(StrictModel):
    session_id: str
    encounter_id: str
    patient_id: str
    status: Literal["QUARANTINED", "DELETED"]


class RecoveryLedger(StrictModel):
    version: Literal[1] = 1
    hospital_id: str
    captured_at: float
    sessions: list[Tombstone]


def provision(db, body: ProvisionInput):
    if body.oidc_issuer != settings.oidc_issuer:
        raise ValueError("Provisioning issuer does not match the configured identity source")
    if len({e.id for e in body.encounters}) != len(body.encounters):
        raise ValueError("Duplicate encounter identifiers")
    if len({u.username for u in body.users}) != len(body.users) or len({u.oidc_subject for u in body.users}) != len(body.users):
        raise ValueError("Duplicate identity mappings")
    for item in body.encounters:
        existing = db.get(Encounter, item.id)
        if existing and (existing.hospital_id, existing.patient_id, existing.admission_id) != (body.hospital_id, item.patient_id, item.admission_id):
            raise ValueError("An existing encounter binding cannot be reassigned")
        if existing:
            changed = False
            for key, value in item.model_dump(exclude={"id", "patient_id", "admission_id"}).items():
                changed = changed or getattr(existing, key) != value
                setattr(existing, key, value)
            if changed:
                existing.source_version += 1
                for session in db.scalars(select(CaptureSession).where(CaptureSession.encounter_id == existing.id)):
                    clinical.invalidate(db, session.id, reason="Hospital encounter details changed during provisioning")
        else:
            db.add(Encounter(hospital_id=body.hospital_id, synthetic=False, **item.model_dump()))
    db.flush()
    for item in body.users:
        for eid in item.encounter_ids:
            encounter = db.get(Encounter, eid)
            if not encounter or encounter.hospital_id != body.hospital_id:
                raise ValueError("Identity grant refers to an unavailable hospital encounter")
        existing = db.scalar(select(User).where(User.username == item.username).with_for_update())
        subject_owner = db.scalar(select(User).where(User.oidc_subject == item.oidc_subject))
        if existing and (existing.hospital_id != body.hospital_id or existing.oidc_subject != item.oidc_subject):
            raise ValueError("An existing account cannot be rebound to another identity")
        if subject_owner and subject_owner is not existing:
            raise ValueError("OIDC subject is already mapped to another account")
        if existing:
            for key, value in item.model_dump(exclude={"username", "oidc_subject"}).items():
                setattr(existing, key, value)
            existing.auth_version += 1
        else:
            db.add(User(hospital_id=body.hospital_id, password_hash="", **item.model_dump()))
    if not db.get(AppSetting, body.hospital_id):
        db.add(AppSetting(key=body.hospital_id, value={"export_enabled": False, "recovery_isolation": False, "audio_retention_hours": settings.audio_retention_hours, "content_retention_days": settings.content_retention_days}))
    db.add(Audit(hospital_id=body.hospital_id, actor_id="offline-operator", action="ops.provision", resource_id=body.hospital_id, detail={"users": len(body.users), "encounters": len(body.encounters)}))
    db.commit()
    return {"users": len(body.users), "encounters": len(body.encounters), "export_enabled": db.get(AppSetting, body.hospital_id).value.get("export_enabled", False)}


def isolate_recovery(db, hospital_id):
    row = db.scalar(select(AppSetting).where(AppSetting.key == hospital_id).with_for_update())
    if not row:
        raise ValueError("Hospital must already exist in the restored database")
    row.value = row.value | {"recovery_isolation": True, "export_enabled": False, "recovery_started_at": now()}
    # Run while all API/worker processes are stopped. Fence pre-restore jobs and tokens.
    db.execute(update(User).where(User.hospital_id == hospital_id).values(active=False, encounter_ids=[], auth_version=User.auth_version + 1))
    sessions = db.scalars(select(CaptureSession).where(CaptureSession.hospital_id == hospital_id).with_for_update()).all()
    for session in sessions:
        session.generation += 1
        if session.status in {"CREATED", "RECORDING", "PAUSED", "FINALIZING"}:
            session.status = "INCOMPLETE"
        notes = db.scalars(select(Note).where(Note.session_id == session.id)).all()
        for note in notes:
            note.review_id = None
            if note.status == "REVIEWED":
                note.status = "DRAFT"
            db.execute(update(Review).where(Review.note_id == note.id).values(valid=False))
    db.execute(update(Job).where(Job.hospital_id == hospital_id, Job.state.in_(["QUEUED", "RUNNING", "RETRY_WAIT"])).values(state="CANCELLED", generation=Job.generation + 1))
    db.execute(update(Export).where(Export.hospital_id == hospital_id, Export.status == "PREPARED").values(status="CANCELLED", active_key=None))
    db.execute(update(Export).where(Export.hospital_id == hospital_id, Export.status == "SENDING").values(status="UNKNOWN"))
    db.add(Audit(hospital_id=hospital_id, actor_id="offline-operator", action="ops.recovery_isolate", resource_id=hospital_id, detail={"sessions": len(sessions)}))
    db.commit()
    return {"recovery_isolation": True, "identities_disabled": True, "sessions": len(sessions)}


def export_ledger(db, hospital_id, output: Path):
    if not db.get(AppSetting, hospital_id):
        raise ValueError("Hospital does not exist")
    sessions = db.scalars(select(CaptureSession).where(CaptureSession.hospital_id == hospital_id, CaptureSession.status.in_(["QUARANTINED", "DELETED"]))).all()
    ledger = RecoveryLedger(hospital_id=hospital_id, captured_at=now(), sessions=[Tombstone(session_id=s.id, encounter_id=s.encounter_id, patient_id=s.patient_id, status=s.status) for s in sessions])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(Fernet(settings.audio_key.encode()).encrypt(ledger.model_dump_json().encode()))
        stream.flush()
        os.fsync(stream.fileno())
    return {"tombstones": len(sessions), "captured_at": ledger.captured_at}


def replay_ledger(db, hospital_id, source: Path):
    try:
        ledger = RecoveryLedger.model_validate_json(Fernet(settings.audio_key.encode()).decrypt(source.read_bytes()))
    except InvalidToken:
        raise ValueError("Recovery ledger cannot be authenticated with the configured key") from None
    if len({s.session_id for s in ledger.sessions}) != len(ledger.sessions):
        raise ValueError("Recovery ledger contains duplicate session identifiers")
    row = db.get(AppSetting, hospital_id)
    if ledger.hospital_id != hospital_id or not row or not row.value.get("recovery_isolation"):
        raise ValueError("Ledger hospital must match an isolated recovery database")
    for item in ledger.sessions:
        session = db.get(CaptureSession, item.session_id)
        if session and (session.hospital_id, session.encounter_id, session.patient_id) != (hospital_id, item.encounter_id, item.patient_id):
            raise ValueError("Restored session identity conflicts with recovery ledger")
    actor = User(username="recovery-" + secrets.token_hex(12), display_name="Offline recovery", hospital_id=hospital_id, roles=["admin"], encounter_ids=list({s.encounter_id for s in ledger.sessions}), active=True)
    db.add(actor)
    db.commit()
    actor_id = actor.id
    counts = {"quarantined": 0, "deleted": 0, "deletion_pending_reconciliation": 0, "absent": 0}
    try:
        for item in ledger.sessions:
            session = db.get(CaptureSession, item.session_id)
            if not session:
                counts["absent"] += 1
                continue
            if session.status not in {"QUARANTINED", "DELETED"}:
                clinical.quarantine(db, actor, session, "Recovery ledger replay")
                db.commit()
            if session.status == "QUARANTINED":
                counts["quarantined"] += 1
            if item.status == "DELETED" or session.status == "DELETED":
                try:
                    lifecycle.purge_session(db, actor, session, "Recovery deletion ledger replay")
                    counts["deleted"] += 1
                except HTTPException as error:
                    if error.detail.get("code") != "export_unresolved":
                        raise
                    db.rollback()
                    counts["deletion_pending_reconciliation"] += 1
        row = db.get(AppSetting, hospital_id)
        row.value = row.value | {"recovery_ledger_captured_at": ledger.captured_at, "recovery_ledger_applied_at": now()}
        db.add(Audit(hospital_id=hospital_id, actor_id=actor.id, action="ops.recovery_replay", resource_id=hospital_id, detail=counts))
        db.commit()
    finally:
        db.rollback()
        actor = db.get(User, actor_id)
        if actor:
            actor.active, actor.encounter_ids = False, []
            db.commit()
    return counts | {"recovery_isolation": True, "window_completeness_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    provision_parser = sub.add_parser("provision")
    provision_parser.add_argument("input", type=Path)
    for name in ["recovery-isolate", "ledger-export", "ledger-replay"]:
        command_parser = sub.add_parser(name)
        command_parser.add_argument("hospital_id")
        if name != "recovery-isolate":
            command_parser.add_argument("file", type=Path)
    args = parser.parse_args()
    create_schema()
    try:
        with SessionLocal() as db:
            if args.command == "provision":
                result = provision(db, ProvisionInput.model_validate_json(args.input.read_bytes()))
            elif args.command == "recovery-isolate":
                result = isolate_recovery(db, args.hospital_id)
            elif args.command == "ledger-export":
                result = export_ledger(db, args.hospital_id, args.file)
            else:
                result = replay_ledger(db, args.hospital_id, args.file)
        print(json.dumps(result))
    except ValidationError:
        parser.exit(2, "Invalid provisioning/ledger schema; input values have been omitted.\n")
    except (ValueError, HTTPException) as error:
        message = error.detail.get("code", "operation_failed") if isinstance(error, HTTPException) else str(error)
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    main()
