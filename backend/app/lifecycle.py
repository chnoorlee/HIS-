from pathlib import Path

from sqlalchemy import select, update

from .audio import event
from .config import settings
from .models import AudioChunk, Export, Fact, FactRevision, Incident, Job, Note, NoteRevision, Review, Transcript, TranscriptRevision, now
from .security import access_session, audit, fail


def purge_session(db, user, session, reason):
    session = access_session(db, user, session.id, allow_quarantined=True, for_update=True)
    if session.status not in {"QUARANTINED", "COMPLETE", "INCOMPLETE", "DELETED"}:
        fail("delete_active_session", "Pause and quarantine active recording before deletion", 409)
    for operation in db.scalars(select(Export).join(Note, Note.id == Export.note_id).where(Note.session_id == session.id)):
        if operation.status in {"SENDING", "UNKNOWN"}:
            fail("export_unresolved", "Reconcile in-flight EMR outcome before deleting its payload", 409)
    counts = {"audio": 0, "transcripts": 0, "facts": 0, "note_revisions": 0, "jobs": 0}
    base = settings.data_dir.resolve()
    for chunk in db.scalars(select(AudioChunk).where(AudioChunk.session_id == session.id)):
        path = Path(chunk.object_path).resolve()
        if path.is_relative_to(base):
            path.unlink(missing_ok=True)
        chunk.status = "DELETED"
        counts["audio"] += 1
    for transcript in db.scalars(select(Transcript).where(Transcript.session_id == session.id)):
        transcript.status, transcript.body = "DELETED", {"text": "", "deleted": True}
        for rev in db.scalars(select(TranscriptRevision).where(TranscriptRevision.transcript_id == transcript.id)):
            rev.body = {"deleted": True}
        counts["transcripts"] += 1
    for fact in db.scalars(select(Fact).where(Fact.session_id == session.id)):
        fact.status, fact.body = "DELETED", {"deleted": True}
        for rev in db.scalars(select(FactRevision).where(FactRevision.fact_id == fact.id)):
            rev.body = {"deleted": True}
        counts["facts"] += 1
    for note in db.scalars(select(Note).where(Note.session_id == session.id)):
        note.status, note.review_id = "QUARANTINED", None
        db.execute(update(Review).where(Review.note_id == note.id).values(valid=False))
        for rev in db.scalars(select(NoteRevision).where(NoteRevision.note_id == note.id)):
            rev.blocks, rev.facts_snapshot = [], {}
            counts["note_revisions"] += 1
        for operation in db.scalars(select(Export).where(Export.note_id == note.id)):
            operation.payload = {"deleted": True, "digest": operation.payload_digest}
            if operation.status == "PREPARED":
                operation.status, operation.active_key = "CANCELLED", None
    for job in db.scalars(select(Job).where(Job.session_id == session.id)):
        job.input, job.result = {"deleted": True}, None
        if job.state in {"QUEUED", "RUNNING", "RETRY_WAIT"}:
            job.state = "CANCELLED"
        job.generation += 1
        counts["jobs"] += 1
    session.status, session.generation = "DELETED", session.generation + 1
    incident = Incident(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, reason=reason, status="LOCAL_DELETED", dependencies={"deleted_counts": counts, "backup_tombstone": True, "provider_deletion": "NOT_VERIFIED" if settings.asr_provider != "unavailable" or settings.model_provider != "demo" else "NOT_APPLICABLE"}, resolution="Local working copies removed. Remote clinical records remain owned by EMR; provider and backup deletion must be verified separately.")
    db.add(incident)
    audit(db, user, "lifecycle.delete", session.id, counts=counts)
    event(db, session, "session.deleted", {"status": "DELETED"})
    db.commit()
    return {"status": "LOCAL_DELETED", "counts": counts, "provider_deletion_verified": False, "backup_restore_tombstone": True}


def retention_sweep(db, user):
    expired = 0
    base = settings.data_dir.resolve()
    for chunk in db.scalars(select(AudioChunk).where(AudioChunk.hospital_id == user.hospital_id, AudioChunk.status == "AVAILABLE", AudioChunk.expires_at < now())):
        path = Path(chunk.object_path).resolve()
        if path.is_relative_to(base):
            path.unlink(missing_ok=True)
        chunk.status = "EXPIRED"
        expired += 1
    audit(db, user, "lifecycle.retention_sweep", user.hospital_id, audio_expired=expired)
    db.commit()
    return {"audio_expired": expired, "content_deletion": "Explicit session deletion applies dependency cleanup; review pending work before disposal."}
