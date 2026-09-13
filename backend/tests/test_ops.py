import json
import time
from types import SimpleNamespace

import pytest
import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app import ops, security
from app.models import AppSetting, AudioChunk, CaptureSession, Encounter, Export, Note, NoteRevision, Review, Transcript, User
from app.ops import ProvisionInput, RecoveryLedger, Tombstone, export_ledger, isolate_recovery, provision, replay_ledger
from conftest import prepared_note


def identity_input(**changes):
    return ProvisionInput.model_validate({
        "hospital_id": "hospital-demo", "oidc_issuer": settings.oidc_issuer,
        "users": [{"username": "oidc-doctor", "oidc_subject": "subject-001", "display_name": "Synthetic doctor", "roles": ["doctor"], "encounter_ids": ["enc-demo-001"], **changes}],
    })


def write_ledger(path, session, status="DELETED", **changes):
    item = Tombstone(session_id=session["id"], encounter_id=session["encounter_id"], patient_id=session["patient_id"], status=status, **changes)
    ledger = RecoveryLedger(hospital_id="hospital-demo", captured_at=1, sessions=[item])
    path.write_bytes(Fernet(settings.audio_key.encode()).encrypt(ledger.model_dump_json().encode()))


def test_provision_requires_explicit_grants_and_preserves_recovery_isolation(client):
    with SessionLocal() as db:
        provision(db, identity_input())
        user = db.scalar(select(User).where(User.username == "oidc-doctor"))
        assert user.active is False and user.password_hash == ""
        old_version = user.auth_version
        isolate_recovery(db, "hospital-demo")
        provision(db, identity_input(active=True, encounter_ids=[]))
        db.refresh(user)
        assert user.active is True and user.encounter_ids == []
        assert user.auth_version > old_version
        value = db.get(AppSetting, "hospital-demo").value
        assert value["recovery_isolation"] is True and value["export_enabled"] is False


@pytest.mark.parametrize("changes", [{"oidc_subject": "other-subject"}, {"encounter_ids": ["missing-encounter"]}])
def test_provision_rejects_rebinding_or_unknown_grants(client, changes):
    with SessionLocal() as db:
        provision(db, identity_input())
        with pytest.raises(ValueError):
            provision(db, identity_input(**changes))
        db.rollback()
        user = db.scalar(select(User).where(User.username == "oidc-doctor"))
        assert user.oidc_subject == "subject-001" and user.encounter_ids == ["enc-demo-001"]


def test_encounter_change_invalidates_exact_review_and_identity_is_immutable(client, doctor, session):
    note, review = prepared_note(client, doctor, session)
    with SessionLocal() as db:
        encounter = db.get(Encounter, session["encounter_id"])
        body = {key: getattr(encounter, key) for key in ["id", "patient_id", "admission_id", "patient_name", "bed", "department", "age", "sex", "admitted_at", "diagnosis"]}
        prior_version = encounter.source_version
        body["diagnosis"] = "Updated hospital source, synthetic only"
        provision(db, ProvisionInput(hospital_id="hospital-demo", oidc_issuer=settings.oidc_issuer, encounters=[body]))
        assert encounter.source_version == prior_version + 1
        assert db.get(Review, review["id"]).valid is False
        assert db.get(Note, note["id"]).review_id is None
        with pytest.raises(ValueError, match="binding"):
            provision(db, ProvisionInput(hospital_id="hospital-demo", oidc_issuer=settings.oidc_issuer, encounters=[body | {"patient_id": "other-patient"}]))
        db.rollback()
        assert db.get(Encounter, session["encounter_id"]).patient_id == session["patient_id"]


def test_recovery_fences_old_tokens_jobs_and_reviews(client, doctor, session):
    note, review = prepared_note(client, doctor, session)
    with SessionLocal() as db:
        old_generation = db.get(CaptureSession, session["id"]).generation
        isolate_recovery(db, "hospital-demo")
        assert db.get(CaptureSession, session["id"]).generation > old_generation
        assert db.get(CaptureSession, session["id"]).status == "INCOMPLETE"
        assert db.get(Review, review["id"]).valid is False
        assert db.get(Note, note["id"]).review_id is None
        assert all(not u.active and not u.encounter_ids for u in db.scalars(select(User)))
    assert client.get("/api/v1/auth/me", headers=doctor).status_code == 401


def test_encrypted_ledger_replay_removes_resurrected_working_content(client, doctor, session, tmp_path):
    note, _ = prepared_note(client, doctor, session)
    client.post(f"/api/v1/sessions/{session['id']}/demo-script", headers=doctor)
    ledger_path = tmp_path / "tombstones.fernet"
    write_ledger(ledger_path, session)
    assert session["patient_id"].encode() not in ledger_path.read_bytes()
    with SessionLocal() as db:
        isolate_recovery(db, "hospital-demo")
        result = replay_ledger(db, "hospital-demo", ledger_path)
        assert result["deleted"] == 1 and not result["window_completeness_verified"]
        assert db.get(CaptureSession, session["id"]).status == "DELETED"
        assert all(t.body.get("deleted") for t in db.scalars(select(Transcript)))
        assert all(r.blocks == [] for r in db.scalars(select(NoteRevision).where(NoteRevision.note_id == note["id"])))
        assert all(not u.active for u in db.scalars(select(User)))
        assert db.get(AppSetting, "hospital-demo").value["recovery_isolation"] is True
        exported = tmp_path / "export.fernet"
        assert export_ledger(db, "hospital-demo", exported)["tombstones"] == 1
        decoded = json.loads(Fernet(settings.audio_key.encode()).decrypt(exported.read_bytes()))
        assert decoded["sessions"][0]["status"] == "DELETED"
        with pytest.raises(FileExistsError):
            export_ledger(db, "hospital-demo", exported)


def test_recovery_keeps_unknown_export_for_reconciliation(client, doctor, admin, session, tmp_path):
    client.patch("/api/v1/admin/settings", headers=admin, json={"mock_emr_scenario": "timeout_after_commit"})
    note, review = prepared_note(client, doctor, session)
    operation = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json={"review_id": review["id"], "idempotency_key": "recovery-unknown-operation"}).json()
    assert operation["status"] == "UNKNOWN"
    ledger_path = tmp_path / "ledger.fernet"
    write_ledger(ledger_path, session)
    with SessionLocal() as db:
        isolate_recovery(db, "hospital-demo")
        result = replay_ledger(db, "hospital-demo", ledger_path)
        assert result["deletion_pending_reconciliation"] == 1
        assert db.get(CaptureSession, session["id"]).status == "QUARANTINED"
        saved = db.get(Export, operation["id"])
        assert saved.status == "UNKNOWN" and not saved.payload.get("deleted")
        assert all(not u.active for u in db.scalars(select(User)))


def test_recovery_rejects_tampered_ledger_and_wrong_binding(client, session, tmp_path):
    ledger_path = tmp_path / "ledger.fernet"
    with SessionLocal() as db:
        isolate_recovery(db, "hospital-demo")
        ledger_path.write_bytes(b"not-an-authenticated-ledger")
        with pytest.raises(ValueError, match="authenticated"):
            replay_ledger(db, "hospital-demo", ledger_path)
        wrong = dict(session) | {"patient_id": "other-patient"}
        write_ledger(ledger_path, wrong)
        with pytest.raises(ValueError, match="identity conflicts"):
            replay_ledger(db, "hospital-demo", ledger_path)
        assert db.get(CaptureSession, session["id"]).status == "INCOMPLETE"


def test_production_recovery_rejects_old_oidc_after_identity_reenabled(client, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(security, "jwks", SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key())))
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "oidc_issuer", "https://identity.hospital.invalid")
    monkeypatch.setattr(settings, "oidc_audience", "inpatient")
    monkeypatch.setattr(ops, "now", lambda: time.time() - 2)
    claims = {"sub": "subject-001", "iss": settings.oidc_issuer, "aud": settings.oidc_audience, "exp": int(time.time()) + 3600}
    with SessionLocal() as db:
        provision(db, identity_input(active=True))
        isolate_recovery(db, "hospital-demo")
        provision(db, identity_input(active=True))
        for payload in [claims, claims | {"iat": int(time.time()) - 60}]:
            with pytest.raises(HTTPException) as error:
                security.decode_user(jwt.encode(payload, private, algorithm="RS256"), db)
            assert error.value.status_code == 401
        fresh = jwt.encode(claims | {"iat": int(time.time())}, private, algorithm="RS256")
        assert security.decode_user(fresh, db).username == "oidc-doctor"


def test_recovery_repurges_audio_from_older_volume_when_database_already_deleted(client, session, tmp_path):
    audio_path = settings.data_dir / "restored-old-audio.bin"
    audio_path.write_bytes(Fernet(settings.audio_key.encode()).encrypt(b"synthetic restored audio"))
    ledger_path = tmp_path / "ledger.fernet"
    write_ledger(ledger_path, session)
    with SessionLocal() as db:
        db.get(CaptureSession, session["id"]).status = "DELETED"
        db.add(AudioChunk(hospital_id="hospital-demo", session_id=session["id"], capture_epoch="restored-epoch", channel_id="doctor_mic", seq=0, sample_start=0, sample_count=160, sha256="0" * 64, object_path=str(audio_path), status="DELETED", expires_at=0))
        db.commit()
        isolate_recovery(db, "hospital-demo")
        assert replay_ledger(db, "hospital-demo", ledger_path)["deleted"] == 1
        assert not audio_path.exists()
