import pytest

from app import clinical
from app.db import SessionLocal
from app.models import CaptureSession, User
from conftest import prepared_note


def editable(note):
    return [{key: block[key] for key in ("key", "title", "text", "fact_ids")} for block in note["blocks"]]


def review(client, doctor, note):
    return client.post(f"/api/v1/notes/{note['id']}/review", headers=doctor,
                       json={"base_revision": note["revision"]})


def allergy_note(client, doctor, session, duplicate=False):
    client.post(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor,
                json={"text": "No drug allergy reported.", "section": "allergies"})
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    note = client.post("/api/v1/notes", headers=doctor,
                       json={"session_id": session["id"], "note_type": "admission"}).json()
    blocks = editable(note)
    for block in blocks:
        block["text"] = "Physician supplied and verified this section."
        if block["key"] == "allergies" or (duplicate and block["key"] == "past_history"):
            block.update(text="Physician verified: no drug allergy.", fact_ids=[fact["id"]])
    note = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor,
                        json={"base_revision": note["revision"], "blocks": blocks}).json()
    assert review(client, doctor, note).status_code == 200
    corrected = client.patch(f"/api/v1/facts/{fact['id']}", headers=doctor,
                             json={"base_revision": 1, "text": "Penicillin allergy confirmed."})
    assert corrected.status_code == 200
    return note, corrected.json()


@pytest.mark.parametrize("edit_other_section", [False, True])
def test_ordinary_save_cannot_clear_stale_physician_reference(client, doctor, session, edit_other_section):
    note, fact = allergy_note(client, doctor, session)
    blocks = editable(note)
    if edit_other_section:
        blocks[0]["text"] += " An unrelated demographic correction."
    saved = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor,
                         json={"base_revision": note["revision"], "blocks": blocks})
    assert saved.status_code == 200
    saved = saved.json()
    assert saved["facts_snapshot"][fact["id"]] == 1
    assert any(issue["code"] == "fact_changed" and issue["block_key"] == "allergies" for issue in saved["issues"])
    assert review(client, doctor, saved).status_code == 409


def test_reference_review_is_explicit_versioned_and_per_section(client, doctor, session):
    note, fact = allergy_note(client, doctor, session, duplicate=True)
    blocks = editable(note)
    allergy = next(block for block in blocks if block["key"] == "allergies")
    allergy.update(text=fact["text"], reference_review={
        "fact_revisions": {fact["id"]: 2}, "reason": "Reconciled the corrected allergy with the patient."})
    saved = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor,
                         json={"base_revision": note["revision"], "blocks": blocks})
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved["facts_snapshot"][fact["id"]] == 1
    changed = [issue for issue in saved["issues"] if issue["code"] == "fact_changed"]
    assert [issue["block_key"] for issue in changed] == ["past_history"]
    assert review(client, doctor, saved).status_code == 409
    blocks = editable(saved)
    history = next(block for block in blocks if block["key"] == "past_history")
    history.update(text=fact["text"], reference_review=allergy["reference_review"])
    saved = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor,
                         json={"base_revision": saved["revision"], "blocks": blocks}).json()
    assert saved["facts_snapshot"][fact["id"]] == 2
    assert review(client, doctor, saved).status_code == 200
    audited = next(block for block in saved["blocks"] if block["key"] == "past_history")
    assert audited["reference_review"]["actor_id"]
    assert audited["reference_review"]["reason"] == allergy["reference_review"]["reason"]


def test_reference_review_rejects_fact_changed_since_dialog(client, doctor, session):
    note, fact = allergy_note(client, doctor, session)
    blocks = editable(note)
    allergy = next(block for block in blocks if block["key"] == "allergies")
    allergy["reference_review"] = {"fact_revisions": {fact["id"]: 1}, "reason": "Stale review window."}
    response = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor,
                            json={"base_revision": note["revision"], "blocks": blocks})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "fact_revision_conflict"
    assert client.get(f"/api/v1/notes/{note['id']}", headers=doctor).json()["revision"] == note["revision"]


@pytest.mark.parametrize("reason", ["   ", " \n\t ", "  x  "])
def test_blank_disposition_cannot_bypass_late_fact_review(client, doctor, session, reason):
    note, _ = prepared_note(client, doctor, session)
    added = client.post(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor,
                        json={"text": "Penicillin allergy reported.", "section": "allergies"})
    assert added.status_code == 201
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    response = client.patch(f"/api/v1/facts/{fact['id']}", headers=doctor, json={
        "base_revision": fact["revision"], "confirmation_status": "excluded", "resolution_reason": reason})
    assert response.status_code == 422
    unchanged = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    assert unchanged["revision"] == fact["revision"]
    assert unchanged["confirmation_status"] == fact["confirmation_status"]
    rejected = review(client, doctor, note)
    assert rejected.status_code == 409
    assert any(issue["code"] == "unincorporated_fact" for issue in rejected.json()["detail"]["issues"])


@pytest.mark.parametrize("disposition", ["include", "exclude"])
def test_late_allergy_requires_disposition_before_review_and_export(client, doctor, session, disposition):
    note, previous_review = prepared_note(client, doctor, session)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="doctor").one()
        clinical.add_transcript(db, user, db.get(CaptureSession, session["id"]),
                                "Penicillin allergy confirmed.", "patient", "patient", "allergies", origin="asr")
        db.commit()
    fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
    rejected = review(client, doctor, note)
    assert rejected.status_code == 409
    assert any(issue["code"] == "unincorporated_fact" for issue in rejected.json()["detail"]["issues"])
    assert client.post(f"/api/v1/notes/{note['id']}/exports", headers=doctor, json={
        "review_id": previous_review["id"], "idempotency_key": "late-allergy-export"}).status_code == 409
    if disposition == "exclude":
        changes = {"confirmation_status": "excluded", "resolution_reason": "Rechecked: this statement belongs to another patient."}
    else:
        changes = {"confirmation_status": "confirmed", "resolution_reason": "Rechecked allergy directly with the patient."}
    corrected = client.patch(f"/api/v1/facts/{fact['id']}", headers=doctor,
                             json={"base_revision": fact["revision"], **changes})
    assert corrected.status_code == 200
    if disposition == "include":
        assert review(client, doctor, note).status_code == 409
        blocks = editable(note)
        next(block for block in blocks if block["key"] == "allergies").update(text=fact["text"], fact_ids=[fact["id"]])
        saved = client.patch(f"/api/v1/notes/{note['id']}", headers=doctor,
                             json={"base_revision": note["revision"], "blocks": blocks})
        assert saved.status_code == 200
        note = saved.json()
    assert review(client, doctor, note).status_code == 200


@pytest.mark.parametrize("subject,section,blocked", [
    ("family", "past_history", False), ("patient", "follow_up", False),
    ("unknown", "allergies", True), ("family", "family_history", True),
])
def test_unincorporated_facts_respect_subject_and_template(client, doctor, session, subject, section, blocked):
    note, _ = prepared_note(client, doctor, session)
    client.post(f"/api/v1/sessions/{session['id']}/transcripts", headers=doctor,
                json={"text": "A newly recorded history statement.", "speaker": "family", "subject": subject, "section": section})
    response = review(client, doctor, note)
    assert response.status_code == (409 if blocked else 200)
