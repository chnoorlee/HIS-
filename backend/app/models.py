import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).timestamp()


class Base(DeclarativeBase):
    pass


class Row:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class User(Row, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(Text, default="")
    hospital_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    roles: Mapped[list] = mapped_column(JSON, default=list)
    encounter_ids: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    oidc_subject: Mapped[str | None] = mapped_column(String(256), unique=True)


class Encounter(Row, Base):
    __tablename__ = "encounters"
    hospital_id: Mapped[str] = mapped_column(String(64), index=True)
    patient_id: Mapped[str] = mapped_column(String(64))
    admission_id: Mapped[str] = mapped_column(String(64))
    patient_name: Mapped[str] = mapped_column(String(128))
    bed: Mapped[str] = mapped_column(String(32))
    department: Mapped[str] = mapped_column(String(128))
    age: Mapped[int] = mapped_column(Integer)
    sex: Mapped[str] = mapped_column(String(32))
    diagnosis: Mapped[str] = mapped_column(Text, default="")
    admitted_at: Mapped[str] = mapped_column(String(40))
    source_version: Mapped[int] = mapped_column(Integer, default=1)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("hospital_id", "id"), UniqueConstraint("hospital_id", "patient_id", "admission_id"))


class Scoped:
    hospital_id: Mapped[str] = mapped_column(String(64), index=True)
    encounter_id: Mapped[str] = mapped_column(String(64), index=True)


class CaptureSession(Row, Scoped, Base):
    __tablename__ = "capture_sessions"
    patient_id: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    final_manifest: Mapped[list] = mapped_column(JSON, default=list)
    source_version: Mapped[int] = mapped_column(Integer)
    consent_record: Mapped[str] = mapped_column(Text, default="synthetic-development")
    expires_at: Mapped[float] = mapped_column(Float, default=lambda: now() + 3600)
    __table_args__ = (ForeignKeyConstraint(["hospital_id", "encounter_id"], ["encounters.hospital_id", "encounters.id"]), UniqueConstraint("hospital_id", "id"))


class StreamTicket(Row, Base):
    __tablename__ = "stream_tickets"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(Integer)
    channels: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class AudioChunk(Row, Base):
    __tablename__ = "audio_chunks"
    hospital_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    capture_epoch: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    sample_start: Mapped[int] = mapped_column(Integer)
    sample_count: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    object_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE")
    expires_at: Mapped[float] = mapped_column(Float)
    clock_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (ForeignKeyConstraint(["hospital_id", "session_id"], ["capture_sessions.hospital_id", "capture_sessions.id"]), UniqueConstraint("hospital_id", "session_id", "capture_epoch", "channel_id", "seq"))


class Source(Row, Scoped, Base):
    __tablename__ = "sources"
    source_system: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE")
    occurred_at: Mapped[str] = mapped_column(String(40))
    recorded_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (ForeignKeyConstraint(["hospital_id", "encounter_id"], ["encounters.hospital_id", "encounters.id"]), UniqueConstraint("hospital_id", "encounter_id", "source_key", "version"))


class Transcript(Row, Scoped, Base):
    __tablename__ = "transcripts"
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE")


class TranscriptRevision(Row, Base):
    __tablename__ = "transcript_revisions"
    transcript_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("transcript_id", "revision"),)


class Fact(Row, Scoped, Base):
    __tablename__ = "facts"
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE")


class FactRevision(Row, Base):
    __tablename__ = "fact_revisions"
    fact_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("fact_id", "revision"),)


class Note(Row, Scoped, Base):
    __tablename__ = "notes"
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    note_type: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    review_id: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (ForeignKeyConstraint(["hospital_id", "encounter_id"], ["encounters.hospital_id", "encounters.id"]),)


class NoteRevision(Row, Base):
    __tablename__ = "note_revisions"
    note_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    blocks: Mapped[list] = mapped_column(JSON)
    facts_snapshot: Mapped[dict] = mapped_column(JSON)
    source_version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64))
    template_version: Mapped[str] = mapped_column(String(64), default="hospital-template-1.0")
    __table_args__ = (UniqueConstraint("note_id", "revision"),)


class Review(Row, Base):
    __tablename__ = "reviews"
    note_id: Mapped[str] = mapped_column(String(64), index=True)
    note_revision: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    facts_snapshot: Mapped[dict] = mapped_column(JSON)
    source_version: Mapped[int] = mapped_column(Integer)
    issue_resolutions: Mapped[list] = mapped_column(JSON)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)


class Job(Row, Scoped, Base):
    __tablename__ = "jobs"
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    input: Mapped[dict] = mapped_column(JSON)
    input_digest: Mapped[str] = mapped_column(String(64), unique=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)


class Export(Row, Scoped, Base):
    __tablename__ = "exports"
    note_id: Mapped[str] = mapped_column(String(64), index=True)
    note_revision: Mapped[int] = mapped_column(Integer)
    review_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="PREPARED")
    target_id: Mapped[str | None] = mapped_column(String(64))
    target_version: Mapped[int | None] = mapped_column(Integer)
    emr_status: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    mapping_version: Mapped[str] = mapped_column(String(64), default="section-map-1.0")
    active_key: Mapped[str | None] = mapped_column(String(192), unique=True)
    __table_args__ = (UniqueConstraint("hospital_id", "idempotency_key"),)


class RemoteDraft(Row, Base):
    __tablename__ = "mock_emr_drafts"
    operation_key: Mapped[str] = mapped_column(String(128), unique=True)
    hospital_id: Mapped[str] = mapped_column(String(64))
    encounter_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    signed: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Row, Scoped, Base):
    __tablename__ = "outbox_events"
    sequence: Mapped[int] = mapped_column(BigInteger, default=lambda: int(now() * 1000000))
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    delivered_at: Mapped[float | None] = mapped_column(Float)


class Audit(Row, Base):
    __tablename__ = "audit"
    hospital_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Incident(Row, Scoped, Base):
    __tablename__ = "incidents"
    session_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    dependencies: Mapped[dict] = mapped_column(JSON, default=dict)
    resolution: Mapped[str | None] = mapped_column(Text)
    external_correction_required: Mapped[bool] = mapped_column(Boolean, default=False)


class AppSetting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
