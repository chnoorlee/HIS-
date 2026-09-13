import httpx
from fastapi import HTTPException
from sqlalchemy import select, update

from .clinical import digest, note_revision, review_issues
from .config import settings
from .db import SessionLocal
from .models import AppSetting, CaptureSession, Export, Incident, Note, RemoteDraft, Review, User
from .security import access_session, audit, fail, require_role


def connector_capabilities():
    return {"provider": settings.emr_provider, "idempotent_create": True, "query_by_operation": True, "automatic_update": False, "signed_record_update": False, "mapping_version": "section-map-1.0", "synthetic": settings.emr_provider == "mock"}


def hospital_settings(db, hospital_id):
    row = db.get(AppSetting, hospital_id)
    return row.value if row else {"export_enabled": False, "recovery_isolation": True}


def _validated_review(db, note, review_id):
    settings_value = hospital_settings(db, note.hospital_id)
    if not settings_value.get("export_enabled") or settings_value.get("recovery_isolation"):
        fail("export_disabled", "Hospital export is disabled or in recovery isolation", 423)
    review = db.get(Review, review_id)
    if not review or not review.valid or review.note_id != note.id or review.note_revision != note.revision or note.review_id != review.id or note.status != "REVIEWED":
        fail("review_invalid", "Export requires the current exact reviewed revision", 409)
    rev = note_revision(db, note)
    if rev.digest != review.digest or rev.source_version != review.source_version:
        fail("review_invalid", "Reviewed content or source snapshot differs", 409)
    unresolved = [i for i in review_issues(db, note) if i["severity"] == "blocker" and not (i["resolvable"] and any(r["issue_id"] == i["id"] for r in review.issue_resolutions))]
    if unresolved:
        fail("review_invalid", "Clinical review has become invalid", 409, issues=unresolved)
    return review, rev


def prepare_export(db, user, note, body):
    # Clinical mutations and export claims share the session lock.
    session = access_session(db, user, note.session_id, for_update=True)
    db.refresh(note)
    existing = db.scalar(select(Export).where(Export.hospital_id == user.hospital_id, Export.idempotency_key == body.idempotency_key))
    if existing:
        if existing.note_id != note.id or existing.review_id != body.review_id:
            fail("idempotency_conflict", "Operation key belongs to a different immutable payload", 409)
        return existing
    review, rev = _validated_review(db, note, body.review_id)
    active = db.scalar(select(Export).where(Export.note_id == note.id, Export.status.in_(["PREPARED", "SENDING", "UNKNOWN"])))
    if active:
        fail("export_inflight", "Previous write result must be reconciled before a new operation", 409, export_id=active.id)
    confirmed = db.scalar(select(Export).where(Export.note_id == note.id, Export.note_revision == note.revision, Export.status == "CONFIRMED"))
    if confirmed:
        fail("already_exported", "This exact note revision has already been confirmed", 409, export_id=confirmed.id)
    payload = {"hospital_id": note.hospital_id, "patient_id": session.patient_id, "encounter_id": note.encounter_id, "note_type": note.note_type, "note_id": note.id, "note_revision": note.revision, "blocks": [{"key": b["key"], "title": b["title"], "text": b["text"]} for b in rev.blocks], "review_digest": review.digest}
    operation = Export(hospital_id=note.hospital_id, encounter_id=note.encounter_id, note_id=note.id, note_revision=note.revision, review_id=review.id, actor_id=user.id, idempotency_key=body.idempotency_key, payload=payload, payload_digest=digest(payload), active_key=note.hospital_id + ":" + note.id)
    db.add(operation)
    db.flush()
    audit(db, user, "export.prepare", operation.id, note_id=note.id, note_revision=note.revision, digest=operation.payload_digest)
    db.commit()
    return operation


def _mock_create(operation):
    with SessionLocal() as remote:
        scenario = hospital_settings(remote, operation.hospital_id).get("mock_emr_scenario", "normal")
        existing = remote.scalar(select(RemoteDraft).where(RemoteDraft.operation_key == operation.idempotency_key))
        if existing:
            if existing.digest != operation.payload_digest:
                return "CONFLICT"
            return "SENT"
        if scenario == "reject":
            return "FAILED"
        if scenario == "signed_conflict":
            return "CONFLICT"
        if scenario in {"timeout_before_commit", "query_not_found"}:
            return "UNKNOWN"
        draft = RemoteDraft(operation_key=operation.idempotency_key, hospital_id=operation.hospital_id, encounter_id=operation.encounter_id, payload=operation.payload, digest=operation.payload_digest)
        remote.add(draft)
        remote.commit()
        return "UNKNOWN" if scenario == "timeout_after_commit" else "SENT"


def _http_create(operation):
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(settings.emr_base_url.rstrip("/") + "/drafts", headers={"Authorization": "Bearer " + settings.emr_api_key, "Idempotency-Key": operation.idempotency_key}, json=operation.payload)
        if response.status_code in {409, 412, 423}:
            return "CONFLICT"
        if response.status_code in {400, 401, 403, 422}:
            return "FAILED"
        return "SENT" if 200 <= response.status_code < 300 else "UNKNOWN"
    except httpx.HTTPError:
        return "UNKNOWN"


def send_export(operation_id):
    with SessionLocal() as db:
        operation = db.get(Export, operation_id)
        if not operation or operation.status != "PREPARED":
            return
        user = db.get(User, operation.actor_id)
        note = db.get(Note, operation.note_id)
        try:
            if not user or not note:
                raise ValueError("Export identity or note is unavailable")
            session = access_session(db, user, note.session_id, for_update=True)
            require_role(user, "doctor", "reviewer")
            db.refresh(note)
            _validated_review(db, note, operation.review_id)
            if note.revision != operation.note_revision:
                raise ValueError("Review invalidated before sending")
            if operation.hospital_id != session.hospital_id or operation.encounter_id != session.encounter_id or operation.payload.get("patient_id") != session.patient_id or digest(operation.payload) != operation.payload_digest:
                raise ValueError("Frozen export binding or payload has changed")
        except (HTTPException, ValueError):
            db.execute(update(Export).where(Export.id == operation_id, Export.status == "PREPARED").values(status="CANCELLED", active_key=None, error="Authorization or review became invalid before sending"))
            db.commit()
            return
        claimed = db.execute(update(Export).where(Export.id == operation_id, Export.status == "PREPARED").values(status="SENDING"))
        if claimed.rowcount != 1:
            return
        db.commit()
    status = _mock_create(operation) if settings.emr_provider == "mock" else _http_create(operation)
    with SessionLocal() as db:
        current = db.get(Export, operation_id)
        if current.status != "SENDING":
            return
        current.status = "UNKNOWN" if status in {"SENT", "UNKNOWN"} else status
        if current.status in {"CONFLICT", "FAILED"}:
            current.active_key = None
        if status == "UNKNOWN":
            current.error = "Remote result is unknown. Read-only reconciliation is required; no automatic resubmission."
        db.commit()
    if status == "SENT":
        reconcile_export(operation_id)


def _read_remote(operation):
    if settings.emr_provider == "mock":
        with SessionLocal() as remote:
            scenario = hospital_settings(remote, operation.hospital_id).get("mock_emr_scenario", "normal")
            if scenario == "query_not_found":
                return None
            draft = remote.scalar(select(RemoteDraft).where(RemoteDraft.operation_key == operation.idempotency_key))
            if not draft:
                return None
            return {"id": draft.id, "payload": draft.payload, "revision": draft.revision, "status": "SIGNED" if draft.signed else "DRAFT"}
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.get(settings.emr_base_url.rstrip("/") + "/operations/" + __import__("urllib.parse", fromlist=["quote"]).quote(operation.idempotency_key, safe=""), headers={"Authorization": "Bearer " + settings.emr_api_key})
        if response.status_code != 200:
            return None
        result = response.json()
        return result if isinstance(result, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def reconcile_export(operation_id):
    with SessionLocal() as db:
        operation = db.get(Export, operation_id)
        if not operation or operation.status not in {"SENDING", "UNKNOWN", "CONFIRMED"}:
            return
    found = _read_remote(operation)
    with SessionLocal() as db:
        current = db.get(Export, operation_id)
        if not found:
            if current.status != "CONFIRMED":
                current.status = "UNKNOWN"
                current.error = "Remote query has not established the result; absence is not proof of failed submission."
        elif not found.get("id") or not isinstance(found.get("revision"), int) or digest(found.get("payload")) != current.payload_digest:
            current.status, current.active_key = "CONFLICT", None
            current.error = "Remote patient, encounter, version or structured content does not match the frozen payload"
        else:
            current.status, current.active_key = "CONFIRMED", None
            current.target_id, current.target_version = str(found["id"]), found["revision"]
            current.emr_status, current.error = found.get("status", "UNKNOWN"), None
            session = db.get(CaptureSession, db.get(Note, current.note_id).session_id)
            if session.status in {"QUARANTINED", "DELETED"}:
                incidents = db.scalars(select(Incident).where(Incident.session_id == session.id)).all()
                for incident in incidents:
                    incident.external_correction_required = True
                    incident.dependencies = incident.dependencies | {"confirmed_export": current.id, "target_id": current.target_id}
        db.commit()


def mock_update_draft(db, draft_id, expected_revision, payload):
    result = db.execute(update(RemoteDraft).where(RemoteDraft.id == draft_id, RemoteDraft.revision == expected_revision, RemoteDraft.signed.is_(False)).values(payload=payload, digest=digest(payload), revision=expected_revision + 1))
    if result.rowcount != 1:
        fail("emr_version_or_signature_conflict", "EMR draft changed or is signed; atomic update refused", 409)
    db.commit()
