from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from app.config import Settings, settings
from app.db import SessionLocal
from app.emr import prepare_export, send_export
from app.models import (
    AudioChunk,
    CaptureSession,
    Event,
    Export,
    Job,
    Note,
    RemoteDraft,
    Review,
    User,
)
from app.schemas import ExportIn
from conftest import prepared_note
from test_protocol import chunk, ticket


def test_transcript_correction_preserves_physician_exclusion(client, doctor, session):
    transcript = client.post(
        f"/api/v1/sessions/{session['id']}/transcripts",
        headers=doctor,
        json={"text": "Excluded background history.", "section": "past_history"},
    ).json()
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[
        0
    ]
    excluded = client.patch(
        f"/api/v1/facts/{fact['id']}",
        headers=doctor,
        json={
            "base_revision": 1,
            "confirmation_status": "excluded",
            "resolution_reason": "Background speaker, not the patient.",
        },
    )
    assert excluded.status_code == 200
    corrected = client.patch(
        f"/api/v1/transcripts/{transcript['id']}",
        headers=doctor,
        json={"base_revision": 1, "text": "Corrected background history."},
    )
    assert corrected.status_code == 200
    saved = client.get(
        f"/api/v1/sessions/{session['id']}/facts", headers=doctor
    ).json()[0]
    assert saved["confirmation_status"] == "excluded"
    assert saved["source_changed"] is True
    note = client.post(
        "/api/v1/notes",
        headers=doctor,
        json={"session_id": session["id"], "note_type": "admission"},
    ).json()
    assert fact["id"] not in note["facts_snapshot"]


def test_null_fact_text_is_rejected_without_damaging_source(client, doctor, session):
    client.post(
        f"/api/v1/sessions/{session['id']}/transcripts",
        headers=doctor,
        json={"text": "Original source."},
    )
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[
        0
    ]
    response = client.patch(
        f"/api/v1/facts/{fact['id']}",
        headers=doctor,
        json={"base_revision": 1, "text": None},
    )
    assert response.status_code == 400
    saved = client.get(
        f"/api/v1/sessions/{session['id']}/facts", headers=doctor
    ).json()[0]
    assert saved["revision"] == 1 and saved["text"] == "Original source."


@pytest.mark.parametrize("change", ["recording", "asr", "fact_extraction", "role"])
def test_export_revalidates_dynamic_blockers_before_first_send(
    client, doctor, session, change
):
    note, review = prepared_note(client, doctor, session)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="doctor").one()
        operation = prepare_export(
            db,
            user,
            db.get(Note, note["id"]),
            ExportIn(
                review_id=review["id"], idempotency_key="prepared-operation-" + change
            ),
        )
        operation_id = operation.id
        if change == "recording":
            db.get(CaptureSession, session["id"]).status = "RECORDING"
        elif change == "role":
            user.roles = ["admin"]
        else:
            db.add(
                Job(
                    hospital_id=user.hospital_id,
                    encounter_id=session["encounter_id"],
                    session_id=session["id"],
                    kind=change,
                    input={},
                    input_digest="pending-" + change,
                    actor_id=user.id,
                )
            )
        db.commit()
    send_export(operation_id)
    with SessionLocal() as db:
        operation = db.get(Export, operation_id)
        assert operation.status == "CANCELLED" and operation.active_key is None
        assert db.query(RemoteDraft).count() == 0


def test_pause_accepts_pending_durable_upload_without_resuming(client, doctor, session):
    value = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        ws.send_json(chunk(session))
        assert ws.receive_json()["type"] == "ACK_DURABLE"
        assert (
            client.post(
                f"/api/v1/sessions/{session['id']}/pause", headers=doctor
            ).status_code
            == 200
        )
        ws.send_json(chunk(session, 1))
        assert ws.receive_json()["type"] == "ACK_DURABLE"
    saved = client.get(f"/api/v1/sessions/{session['id']}", headers=doctor).json()
    assert saved["status"] == "PAUSED"
    manifest = client.get(
        f"/api/v1/sessions/{session['id']}/audio-manifest", headers=doctor
    ).json()
    assert len(manifest["chunks"]) == 2


def test_capture_metadata_is_bounded_and_persisted(client, doctor, session):
    value = ticket(client, doctor, session)
    metadata = {
        "input_sample_rate": 48000,
        "input_channels": 2,
        "device_id": "test-device",
        "resampler": "verified-test-resampler",
        "client_version": "1.0",
    }
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        ws.send_json(
            chunk(session) | {"capture_metadata": metadata | {"input_channels": 9}}
        )
        assert ws.receive_json()["type"] == "ERROR"
        ws.send_json(chunk(session) | {"capture_metadata": metadata})
        assert ws.receive_json()["type"] == "ACK_DURABLE"
    with SessionLocal() as db:
        saved = db.query(AudioChunk).one()
        assert saved.clock_metadata["capture_metadata"] == metadata


def test_deletion_invalidates_review_and_cancels_unsent_export(
    client, doctor, admin, session
):
    note, review = prepared_note(client, doctor, session)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="doctor").one()
        operation = prepare_export(
            db,
            user,
            db.get(Note, note["id"]),
            ExportIn(
                review_id=review["id"], idempotency_key="delete-prepared-operation"
            ),
        )
        operation_id = operation.id
        db.get(CaptureSession, session["id"]).status = "COMPLETE"
        db.commit()
    response = client.post(
        f"/api/v1/admin/sessions/{session['id']}/delete",
        headers=admin,
        json={"reason": "Delete synthetic working copies."},
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.get(Review, review["id"]).valid is False
        operation = db.get(Export, operation_id)
        assert operation.status == "CANCELLED" and operation.active_key is None
    send_export(operation_id)
    with SessionLocal() as db:
        assert db.query(RemoteDraft).count() == 0


def test_session_patient_binding_is_rechecked(client, doctor, session):
    with SessionLocal() as db:
        db.get(CaptureSession, session["id"]).patient_id = "another-patient"
        db.commit()
    response = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "binding_mismatch"


def test_session_events_remain_ordered_when_clock_repeats(
    client, doctor, session, monkeypatch
):
    from app import audio

    monkeypatch.setattr(audio, "now", lambda: 1.0)
    with SessionLocal() as db:
        capture = db.get(CaptureSession, session["id"])
        first = audio.event(db, capture, "test.first")
        second = audio.event(db, capture, "test.second", event_id="test-second-event")
        db.commit()
        assert second.created_at > first.created_at
        assert second.sequence > first.sequence
        assert second.id == "test-second-event"


def test_event_sequence_migration_preserves_existing_64_bit_cursor(
    tmp_path, monkeypatch
):
    database_url = "sqlite:///" + (tmp_path / "migration.db").as_posix()
    local_engine = sa.create_engine(database_url)
    old_metadata = sa.MetaData()
    old_events = Event.__table__.to_metadata(old_metadata)
    old_events.c.sequence.type = sa.Integer()
    old_metadata.create_all(local_engine)
    cursor = 1788941234567890
    with local_engine.begin() as connection:
        connection.execute(
            old_events.insert().values(
                id="existing-event",
                sequence=cursor,
                hospital_id="test-hospital",
                encounter_id="test-encounter",
                session_id="test-session",
                kind="test.event",
                payload={},
            )
        )
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    command.stamp(config, "0001_initial")
    command.upgrade(config, "head")
    assert isinstance(
        next(
            c
            for c in sa.inspect(local_engine).get_columns("outbox_events")
            if c["name"] == "sequence"
        )["type"],
        sa.BigInteger,
    )
    with local_engine.connect() as connection:
        assert (
            connection.execute(sa.select(old_events.c.sequence)).scalar_one() == cursor
        )
    local_engine.dispose()


@pytest.mark.parametrize(
    "override",
    [
        {"asr_base_url": "ws://approved.example/realtime"},
        {"asr_base_url": "wss://other.example/realtime"},
        {"asr_base_url": "wss://user@approved.example/realtime"},
        {"asr_api_key": ""},
        {"asr_model": ""},
    ],
)
def test_realtime_configuration_rejects_unapproved_or_incomplete_connection(
    tmp_path, override
):
    values = {
        "env": "test",
        "data_dir": tmp_path,
        "asr_provider": "dashscope_realtime",
        "asr_base_url": "wss://approved.example/realtime",
        "approved_model_hosts": "approved.example",
        "asr_api_key": "synthetic-test-key",
        "asr_model": "explicit-model",
    }
    with pytest.raises(RuntimeError):
        Settings(_env_file=None, **(values | override)).prepare()


@pytest.mark.parametrize(
    "override",
    [
        {"emr_api_key": ""},
        {"emr_base_url": "https:///missing-host"},
        {"oidc_issuer": "http://identity.example"},
        {"allowed_origins": "http://localhost:5173"},
    ],
)
def test_production_configuration_rejects_unsafe_endpoints(tmp_path, override):
    values = {
        "env": "production",
        "data_dir": tmp_path,
        "database_url": "postgresql://localhost/his",
        "jwt_secret": "synthetic-test-secret-" * 3,
        "audio_key": Fernet.generate_key().decode(),
        "model_provider": "unavailable",
        "emr_provider": "http",
        "emr_base_url": "https://emr.example",
        "emr_api_key": "synthetic-test-key",
        "oidc_issuer": "https://identity.example",
        "oidc_jwks_url": "https://identity.example/jwks",
        "oidc_audience": "his",
        "allowed_origins": "https://hospital.example",
    }
    with pytest.raises(RuntimeError):
        Settings(_env_file=None, **(values | override)).prepare()
