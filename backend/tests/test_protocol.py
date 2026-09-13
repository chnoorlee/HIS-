import base64
import hashlib

import pytest
from starlette.websockets import WebSocketDisconnect

from app.db import SessionLocal
from app.models import AudioChunk, CaptureSession, now


def ticket(client, doctor, session):
    response = client.post(f"/api/v1/sessions/{session['id']}/stream-ticket", headers=doctor, json={"device_id": "test-device", "channels": [{"channel_id": "doctor_mic", "capture_epoch": "epoch-test"}]})
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def chunk(session, seq=0, raw=None, start=None):
    raw = raw if raw is not None else b"\x01\x00" * 1600
    return {"type": "chunk", "protocol_version": 1, "session_id": session["id"], "capture_epoch": "epoch-test", "channel_id": "doctor_mic", "seq": seq, "sample_start": start if start is not None else seq * 1600, "sample_count": len(raw) // 2, "sample_rate": 16000, "encoding": "pcm_s16le", "sha256": hashlib.sha256(raw).hexdigest(), "data": base64.b64encode(raw).decode()}


def test_durable_encrypted_replay_gap_and_finalization(client, doctor, session):
    value = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        ws.send_json(chunk(session, 1))
        ack = ws.receive_json()
        assert ack["type"] == "ACK_DURABLE" and ack["contiguous_seq"] == -1 and ack["gaps"] == [0]
        ws.send_json(chunk(session))
        assert ws.receive_json()["contiguous_seq"] == 1
        ws.send_json(chunk(session))
        assert ws.receive_json()["type"] == "ACK_DURABLE"
    manifest = client.get(f"/api/v1/sessions/{session['id']}/audio-manifest", headers=doctor).json()
    assert len(manifest["chunks"]) == 2
    with SessionLocal() as db:
        chunks = db.query(AudioChunk).all()
        assert len(chunks) == 2
        assert __import__("pathlib").Path(chunks[0].object_path).read_bytes() != b"\x01\x00" * 1600
    audio = client.get(f"/api/v1/sessions/{session['id']}/audio", params={"channel_id": "doctor_mic", "capture_epoch": "epoch-test", "sample_end": 3200}, headers=doctor)
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    final = client.post(f"/api/v1/sessions/{session['id']}/finalize", headers=doctor, json={"channels": [{"channel_id": "doctor_mic", "capture_epoch": "epoch-test", "last_seq": 1, "sample_end": 3200}]})
    assert final.status_code == 200 and final.json()["status"] == "COMPLETE"


def test_single_use_ticket_and_generation_fence(client, doctor, session):
    old = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + old) as ws:
        ticket(client, doctor, session)
        ws.send_json(chunk(session))
        assert ws.receive_json()["code"] == "stale_connection"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/audio?ticket=" + old):
            pass


def test_conflicting_same_sequence_quarantines_all_material(client, doctor, session):
    value = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        ws.send_json(chunk(session))
        assert ws.receive_json()["type"] == "ACK_DURABLE"
        ws.send_json(chunk(session, raw=b"\x02\x00" * 1600))
        assert ws.receive_json()["code"] == "chunk_conflict"
    assert client.get(f"/api/v1/sessions/{session['id']}/audio-manifest", headers=doctor).status_code == 423


def test_binding_checksum_and_sample_discontinuity(client, doctor, session):
    value = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        bad = chunk(session)
        bad["sha256"] = "0" * 64
        ws.send_json(bad)
        assert ws.receive_json()["code"] == "checksum_mismatch"
        ws.send_json(chunk(session, start=1))
        assert ws.receive_json()["code"] == "sample_discontinuity"
        bad = chunk(session)
        bad["session_id"] = "different-patient-session"
        ws.send_json(bad)
        assert ws.receive_json()["code"] == "binding_mismatch"


def test_missing_audio_never_marked_complete(client, doctor, session):
    value = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        ws.send_json(chunk(session, 1))
        ws.receive_json()
    final = client.post(f"/api/v1/sessions/{session['id']}/finalize", headers=doctor, json={"channels": [{"channel_id": "doctor_mic", "capture_epoch": "epoch-test", "last_seq": 1, "sample_end": 3200}]})
    assert final.json()["status"] == "INCOMPLETE"
    assert client.post(f"/api/v1/sessions/{session['id']}/finalize", headers=doctor, json={"channels": []}).status_code == 409


def test_expired_audio_unavailable_and_gap_ledger_idempotent(client, doctor, session):
    value = ticket(client, doctor, session)
    with client.websocket_connect("/api/v1/audio?ticket=" + value) as ws:
        ws.send_json(chunk(session))
        ws.receive_json()
    with SessionLocal() as db:
        db.query(AudioChunk).update({"expires_at": now() - 1})
        db.commit()
    response = client.get(f"/api/v1/sessions/{session['id']}/audio", params={"channel_id": "doctor_mic", "capture_epoch": "epoch-test"}, headers=doctor)
    assert response.status_code == 410
    gap = {"gaps": [{"channel_id": "doctor_mic", "capture_epoch": "epoch-test", "seq": 1, "sample_start": 1600, "sample_count": 1600, "reason": "local_retention_expired"}]}
    for _ in range(2):
        result = client.post(f"/api/v1/sessions/{session['id']}/audio-gaps", headers=doctor, json=gap)
        assert result.status_code == 200
