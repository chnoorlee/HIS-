import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.emr import mock_update_draft
from app.models import AudioChunk, Fact, RemoteDraft, Transcript
from conftest import prepared_note


def test_patient_scoping_admin_controls_and_auth(client, doctor, session):
    assert client.get("/api/v1/encounters").status_code == 401
    token = client.post("/api/v1/auth/login", json={"username": "restricted", "password": "Restricted123!"}).json()["access_token"]
    restricted = {"Authorization": "Bearer " + token}
    assert client.get("/api/v1/encounters/enc-demo-001", headers=restricted).status_code == 403
    assert client.get(f"/api/v1/sessions/{session['id']}/facts", headers=restricted).status_code == 403
    assert client.get("/api/v1/admin/settings", headers=doctor).status_code == 403
    assert client.get("/api/v1/audit", headers=doctor).status_code == 403


def test_normal_export_idempotence_and_signed_draft_cas(client, doctor, session):
    note, review = prepared_note(client, doctor, session)
    body = {"review_id": review["id"], "idempotency_key": "test-normal-operation"}
    first = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json=body)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "CONFIRMED"
    repeated = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json=body).json()
    assert repeated["id"] == first.json()["id"]
    with SessionLocal() as db:
        assert db.query(RemoteDraft).count() == 1
        remote = db.query(RemoteDraft).one()
        remote.signed = True
        db.commit()
        with pytest.raises(HTTPException) as caught:
            mock_update_draft(db, remote.id, remote.revision, {"tampered": True})
        assert caught.value.status_code == 409
        db.rollback()
        assert db.get(RemoteDraft, remote.id).payload.get("tampered") is None


def test_remote_commit_timeout_unknown_blocks_new_operation_then_reconciles(client, doctor, admin, session):
    client.patch("/api/v1/admin/settings", headers=admin, json={"mock_emr_scenario": "timeout_after_commit"})
    note, review = prepared_note(client, doctor, session)
    body = {"review_id": review["id"], "idempotency_key": "timeout-committed-operation"}
    operation = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json=body).json()
    assert operation["status"] == "UNKNOWN"
    second = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json=body | {"idempotency_key": "another-operation"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "export_inflight"
    reconciled = client.post(f"/api/v1/exports/{operation['id']}/reconcile", headers=doctor).json()
    assert reconciled["status"] == "CONFIRMED"
    with SessionLocal() as db:
        assert db.query(RemoteDraft).count() == 1


def test_query_not_found_is_not_permission_to_resubmit(client, doctor, admin, session):
    client.patch("/api/v1/admin/settings", headers=admin, json={"mock_emr_scenario": "query_not_found"})
    note, review = prepared_note(client, doctor, session)
    operation = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json={"review_id": review["id"], "idempotency_key": "not-found-unknown-operation"}).json()
    for _ in range(2):
        value = client.post(f"/api/v1/exports/{operation['id']}/reconcile", headers=doctor).json()
        assert value["status"] == "UNKNOWN"
    with SessionLocal() as db:
        assert db.query(RemoteDraft).count() == 0


def test_quarantine_blocks_content_but_reconciles_existing_remote_write(client, doctor, admin, session):
    client.patch("/api/v1/admin/settings", headers=admin, json={"mock_emr_scenario": "timeout_after_commit"})
    note, review = prepared_note(client, doctor, session)
    operation = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json={"review_id": review["id"], "idempotency_key": "quarantine-inflight-operation"}).json()
    assert operation["status"] == "UNKNOWN"
    assert client.post(f"/api/v1/sessions/{session['id']}/quarantine", headers=doctor, json={"reason": "模拟发现误绑定，需要隔离调查"}).status_code == 200
    assert client.get(f"/api/v1/notes/{note['id']}", headers=doctor).status_code == 423
    reconciled = client.post(f"/api/v1/exports/{operation['id']}/reconcile", headers=admin).json()
    assert reconciled["status"] == "CONFIRMED" and "payload" not in reconciled
    incidents = client.get("/api/v1/admin/incidents", headers=admin).json()
    assert any(i["external_correction_required"] for i in incidents)


def test_revoked_permission_blocks_already_queued_job(client, doctor, admin, session):
    from app.jobs import claim_job, execute_job
    from app.models import Job
    note, _ = prepared_note(client, doctor, session)
    job = client.post(f"/api/v1/notes/{note['id']}/suggestions", headers=doctor, json={"base_revision": note["revision"]}).json()
    claim = claim_job(["suggestions"])
    me = client.get("/api/v1/auth/me", headers=doctor).json()
    client.patch(f"/api/v1/admin/users/{me['id']}/grants", headers=admin, json={"encounter_ids": []})
    execute_job(*claim)
    with SessionLocal() as db:
        saved = db.get(Job, job["id"])
        assert saved.state == "FAILED" and saved.result is None
    assert client.get(f"/api/v1/sessions/{session['id']}", headers=doctor).status_code == 401


def test_retention_deletes_derivatives_and_keeps_tombstone(client, doctor, admin, session):
    note, review = prepared_note(client, doctor, session)
    client.post(f"/api/v1/sessions/{session['id']}/demo-script", headers=doctor)
    client.post(f"/api/v1/sessions/{session['id']}/quarantine", headers=doctor, json={"reason": "模拟误录，授权删除工作副本"})
    result = client.post(f"/api/v1/admin/sessions/{session['id']}/delete", headers=admin, json={"reason": "模拟保留策略到期，删除工作副本"})
    assert result.status_code == 200, result.text
    assert result.json()["backup_restore_tombstone"]
    with SessionLocal() as db:
        assert all(t.body.get("deleted") for t in db.query(Transcript).all())
        assert all(f.body.get("deleted") for f in db.query(Fact).all())
    assert client.get(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor).status_code == 423
