import pytest

from app.clinical import TEMPLATES
from app.db import SessionLocal
from app.jobs import claim_job, execute_job
from app.models import Fact, Job, NoteRevision, Review
from app.providers import ProviderError, suggest
from conftest import prepared_note


@pytest.mark.parametrize("note_type", list(TEMPLATES))
def test_four_templates_missing_sources_block_review(client, doctor, session, note_type):
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": note_type}).json()
    assert all(not b["text"] for b in note["blocks"])
    response = client.post(f"/api/v1/notes/{note['id']}/review", headers=doctor, json={"base_revision": 1})
    assert response.status_code == 409
    assert all(i["code"] == "missing_section" for i in response.json()["detail"]["issues"])


def test_manual_revision_is_immutable_and_old_review_invalid(client, doctor, session):
    note, review = prepared_note(client, doctor, session)
    original = note["blocks"][0]["text"]
    blocks = [{k: b[k] for k in ["key", "title", "text", "fact_ids"]} for b in note["blocks"]]
    blocks[0]["text"] = "医生更正：患者尚未核实相关信息。"
    response = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor, json={"base_revision": note["revision"], "blocks": blocks})
    assert response.status_code == 200
    assert response.json()["blocks"][0]["protected"]
    conflict = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor, json={"base_revision": note["revision"], "blocks": blocks})
    assert conflict.status_code == 409
    with SessionLocal() as db:
        assert db.get(Review, review["id"]).valid is False
        previous = db.query(NoteRevision).filter_by(note_id=note["id"], revision=note["revision"]).one()
        assert previous.blocks[0]["text"] == original


def test_family_subject_not_used_as_patient_history(client, doctor, session):
    client.post(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor, json={"text": "我自己有糖尿病，患者没有说这件事。", "speaker": "family", "subject": "family", "section": "past_history"})
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": "admission"}).json()
    assert next(b for b in note["blocks"] if b["key"] == "past_history")["text"] == ""
    assert client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]["subject"] == "family"


def test_role_correction_invalidates_fact_and_dependent_review(client, doctor, session):
    transcript = client.post(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor, json={"text": "患者既往有高血压。", "speaker": "doctor", "subject": "patient", "section": "past_history"}).json()
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    response = client.patch(f"/api/v1/transcripts/{transcript['id']}", headers=doctor, json={"base_revision": 1, "speaker": "family", "subject": "family"})
    assert response.status_code == 200
    fact2 = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    assert fact2["revision"] == 2 and fact2["confirmation_status"] == "unconfirmed" and fact2["source_changed"]
    assert client.patch(f"/api/v1/facts/{fact['id']}", headers=doctor, json={"base_revision": 2, "confirmation_status": "confirmed"}).status_code == 400


def test_unknown_dose_and_negation_preserved_verbatim(client, doctor, session):
    text = "药名记不清，每次半片，不知道毫克数；未询问青霉素过敏，不能记为否认。"
    client.post(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor, json={"text": text, "speaker": "doctor", "subject": "patient", "section": "medication_history"})
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    assert fact["text"] == text and fact["normalized_value"] is None and fact["polarity"] == "unknown"
    assert fact["evidence"][0]["quote"] == text and fact["evidence"][0]["audio_range"] is None


def test_stale_generation_job_never_overwrites_physician_blocks(client, doctor, session):
    note, _ = prepared_note(client, doctor, session)
    response = client.post(f"/api/v1/notes/{note['id']}/suggestions", headers=doctor, json={"base_revision": note["revision"], "sections": []})
    assert response.status_code == 202
    claimed = claim_job(["suggestions"])
    blocks = [{k: b[k] for k in ["key", "title", "text", "fact_ids"]} for b in note["blocks"]]
    blocks[0]["text"] = "医生正在书写的新版本"
    client.patch(f"/api/v1/notes/{note['id']}", headers=doctor, json={"base_revision": note["revision"], "blocks": blocks})
    execute_job(*claimed)
    job = client.get("/api/v1/jobs/" + response.json()["id"], headers=doctor).json()
    assert job["state"] == "STALE"
    assert client.get(f"/api/v1/notes/{note['id']}", headers=doctor).json()["blocks"][0]["text"] == "医生正在书写的新版本"


def test_expired_worker_generation_cannot_publish(client, doctor, session):
    note, _ = prepared_note(client, doctor, session)
    job = client.post(f"/api/v1/notes/{note['id']}/suggestions", headers=doctor, json={"base_revision": note["revision"]}).json()
    claimed = claim_job(["suggestions"])
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        row.generation += 1
        db.commit()
    execute_job(*claimed)
    with SessionLocal() as db:
        assert db.get(Job, job["id"]).result is None


def test_source_refresh_invalidates_review(client, doctor, session):
    note, review = prepared_note(client, doctor, session)
    assert client.post("/api/v1/encounters/enc-demo-001/sources/refresh", headers=doctor).status_code == 200
    response = client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json={"review_id": review["id"], "idempotency_key": "source-change-export"})
    assert response.status_code == 409
