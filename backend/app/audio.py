import base64
import hashlib
import io
import os
import secrets
import wave
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, update

from .config import settings
from .models import AudioChunk, CaptureSession, Event, StreamTicket, now
from .security import access_session, audit, fail


def event(db, session, kind, payload=None, event_id=None):
    # Keep each session's event timestamps in commit order, including concurrent writers.
    with db.no_autoflush:
        db.scalar(select(CaptureSession).where(CaptureSession.id == session.id).with_for_update())
    previous = db.scalar(select(func.max(Event.created_at)).where(Event.session_id == session.id))
    created_at = max(now(), previous + 0.000001 if previous is not None else 0)
    row = Event(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, kind=kind, payload=payload or {}, created_at=created_at, sequence=int(created_at * 1000000))
    if event_id is not None:
        row.id = event_id
    db.add(row)
    return row


def issue_ticket(db, user, session, body):
    if session.status not in {"CREATED", "RECORDING", "PAUSED", "INCOMPLETE", "FINALIZING"}:
        fail("invalid_state", "Audio session does not accept connections", 409)
    if session.expires_at < now():
        fail("authorization_expired", "Recording authorization has expired", 403)
    channels = [c if isinstance(c, str) else c.channel_id for c in body.channels]
    if len(set(channels)) != len(channels) or any(not c or len(c) > 64 or not all(x.isalnum() or x in "-_" for x in c) for c in channels):
        fail("invalid_channels", "Channel identifiers must be unique and valid")
    if session.channels and set(channels) != set(session.channels):
        fail("channel_binding_conflict", "Channel identity is fixed for this session", 409)
    generation = session.generation + 1
    result = db.execute(update(CaptureSession).where(CaptureSession.id == session.id, CaptureSession.generation == session.generation).values(generation=generation, channels=channels))
    if result.rowcount != 1:
        fail("generation_conflict", "Reconnect generation changed; retry", 409)
    token = secrets.token_urlsafe(48)
    expiry = now() + 60
    db.add(StreamTicket(token_hash=hashlib.sha256(token.encode()).hexdigest(), session_id=session.id, user_id=user.id, device_id=body.device_id, generation=generation, channels=channels, expires_at=expiry))
    audit(db, user, "audio.ticket", session.id, generation=generation)
    db.commit()
    return {"ticket": token, "session_id": session.id, "expires_at": expiry, "generation": generation, "ws_url": "/api/v1/audio?ticket=" + token}


def consume_ticket(db, token):
    ticket = db.scalar(select(StreamTicket).where(StreamTicket.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not ticket or ticket.consumed or ticket.expires_at < now():
        fail("ticket_invalid", "Stream ticket is invalid, expired or already used", 401)
    result = db.execute(update(StreamTicket).where(StreamTicket.id == ticket.id, StreamTicket.consumed.is_(False), StreamTicket.expires_at > now()).values(consumed=True))
    if result.rowcount != 1:
        fail("ticket_consumed", "Stream ticket was already consumed", 401)
    db.commit()
    return ticket


def chunk_json(chunk):
    return {k: getattr(chunk, k) for k in ["id", "session_id", "capture_epoch", "channel_id", "seq", "sample_start", "sample_count", "sha256", "status", "expires_at"]}


def manifest(db, session):
    chunks = db.scalars(select(AudioChunk).where(AudioChunk.session_id == session.id).order_by(AudioChunk.channel_id, AudioChunk.capture_epoch, AudioChunk.seq)).all()
    groups = defaultdict(list)
    for chunk in chunks:
        groups[(chunk.channel_id, chunk.capture_epoch)].append(chunk)
    channels = []
    for (channel, epoch), values in groups.items():
        seqs = {c.seq for c in values}
        contiguous = -1
        end = 0
        by_seq = {c.seq: c for c in values}
        while contiguous + 1 in seqs:
            following = by_seq[contiguous + 1]
            if following.sample_start != end:
                break
            contiguous += 1
            end = following.sample_start + following.sample_count
        maximum = max(seqs)
        gaps = [n for n in range(maximum + 1) if n not in seqs]
        channels.append({"channel_id": channel, "capture_epoch": epoch, "contiguous_seq": contiguous, "gaps": gaps, "received_seq": sorted(seqs), "sample_end": max(c.sample_start + c.sample_count for c in values), "contiguous_sample_end": end})
    return {"session_id": session.id, "status": session.status, "channels": channels, "chunks": [chunk_json(c) for c in chunks], "final_manifest": session.final_manifest}


def store_chunk(db, user, ticket, body):
    # A session row lock serializes range checks with finalization on PostgreSQL.
    session = db.scalar(select(CaptureSession).where(CaptureSession.id == ticket.session_id).with_for_update())
    access_session(db, user, session.id)
    if body.session_id != session.id or (body.hospital_id and body.hospital_id != session.hospital_id):
        fail("binding_mismatch", "Audio identity does not match the authorized session", 403)
    if ticket.generation != session.generation:
        fail("stale_connection", "A newer connection replaced this stream", 409)
    if session.expires_at < now():
        fail("authorization_expired", "Session recording authorization expired", 403)
    if body.channel_id not in ticket.channels:
        fail("channel_forbidden", "Channel is not authorized", 403)
    if session.status == "COMPLETE":
        fail("invalid_state", "Session is complete", 409)
    try:
        raw = base64.b64decode(body.data, validate=True)
    except ValueError:
        fail("invalid_audio", "Audio must be valid base64")
    if len(raw) != body.sample_count * 2 or len(raw) > settings.max_audio_chunk_bytes:
        fail("invalid_audio_size", "PCM byte count does not match declared samples")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != body.sha256:
        fail("checksum_mismatch", "Audio checksum verification failed")
    existing = db.scalar(select(AudioChunk).where(AudioChunk.session_id == session.id, AudioChunk.channel_id == body.channel_id, AudioChunk.capture_epoch == str(body.capture_epoch), AudioChunk.seq == body.seq))
    if existing:
        if (existing.sha256, existing.sample_start, existing.sample_count) != (digest, body.sample_start, body.sample_count):
            from .clinical import quarantine
            quarantine(db, user, session, "Same audio sequence was submitted with conflicting content or coordinates")
            db.commit()
            fail("chunk_conflict", "Conflicting audio sequence quarantined the session", 409)
        if existing.status != "AVAILABLE" or existing.expires_at < now() or not __import__("pathlib").Path(existing.object_path).is_file():
            fail("audio_unavailable", "Previously stored audio is no longer available", 410)
    else:
        ranges = db.scalars(select(AudioChunk).where(AudioChunk.session_id == session.id, AudioChunk.channel_id == body.channel_id, AudioChunk.capture_epoch == str(body.capture_epoch))).all()
        if body.seq == 0 and body.sample_start != 0:
            fail("sample_discontinuity", "First sequence must start at sample zero", 409)
        for prior in ranges:
            if max(prior.sample_start, body.sample_start) < min(prior.sample_start + prior.sample_count, body.sample_start + body.sample_count):
                fail("sample_overlap", "Audio sample ranges overlap", 409)
            if prior.seq + 1 == body.seq and prior.sample_start + prior.sample_count != body.sample_start:
                fail("sample_discontinuity", "Adjacent audio sequences must have contiguous samples", 409)
            if body.seq + 1 == prior.seq and body.sample_start + body.sample_count != prior.sample_start:
                fail("sample_discontinuity", "Adjacent audio sequences must have contiguous samples", 409)
        if session.final_manifest:
            expected = next((c for c in session.final_manifest if c["channel_id"] == body.channel_id and str(c["capture_epoch"]) == str(body.capture_epoch)), None)
            if not expected or body.seq > expected["last_seq"] or body.sample_start + body.sample_count > expected["sample_end"]:
                fail("final_boundary", "Chunk exceeds declared recording endpoint", 409)
        target_dir = settings.data_dir / "audio" / session.hospital_id / session.id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / (secrets.token_hex(16) + ".enc")
        encrypted = Fernet(settings.audio_key.encode()).encrypt(raw)
        with path.open("xb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            descriptor = os.open(target_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        from .emr import hospital_settings
        retention_hours = hospital_settings(db, session.hospital_id).get("audio_retention_hours", settings.audio_retention_hours)
        db.add(AudioChunk(hospital_id=session.hospital_id, session_id=session.id, capture_epoch=str(body.capture_epoch), channel_id=body.channel_id, seq=body.seq, sample_start=body.sample_start, sample_count=body.sample_count, sha256=digest, object_path=str(path.resolve()), expires_at=now() + retention_hours * 3600, clock_metadata={"monotonic_ticks": body.monotonic_ticks, "clock_uncertainty_ms": body.clock_uncertainty_ms, "capture_metadata": body.capture_metadata.model_dump(exclude_none=True) if body.capture_metadata else None}))
        if session.status == "CREATED":
            session.status = "RECORDING"
        event(db, session, "audio.durable", {"channel_id": body.channel_id, "capture_epoch": str(body.capture_epoch), "seq": body.seq})
    db.commit()
    state = manifest(db, session)
    waterline = next(c for c in state["channels"] if c["channel_id"] == body.channel_id and c["capture_epoch"] == str(body.capture_epoch))
    return {"type": "ACK_DURABLE", "session_id": session.id, "sha256": digest, "channel_id": body.channel_id, "capture_epoch": str(body.capture_epoch), "seq": body.seq, "contiguous_seq": waterline["contiguous_seq"], "gaps": waterline["gaps"]}


def finalize(db, user, session, body):
    session = access_session(db, user, session.id, for_update=True)
    if session.status == "COMPLETE":
        if body.channels and [c.model_dump() | {"capture_epoch": str(c.capture_epoch)} for c in body.channels] != session.final_manifest:
            fail("final_boundary_conflict", "Completed session endpoint is immutable", 409)
        return manifest(db, session)
    if session.status not in {"CREATED", "RECORDING", "PAUSED", "INCOMPLETE", "FINALIZING"}:
        fail("invalid_state", "Session cannot be finalized", 409)
    declared = [c.model_dump() | {"capture_epoch": str(c.capture_epoch)} for c in body.channels]
    if len({(c["channel_id"], c["capture_epoch"]) for c in declared}) != len(declared):
        fail("duplicate_boundary", "Final channel endpoints must be unique")
    actual = manifest(db, session)
    complete = True
    issues = []
    actual_keys = {(c["channel_id"], c["capture_epoch"]) for c in actual["channels"]}
    declared_keys = {(c["channel_id"], c["capture_epoch"]) for c in declared}
    if actual_keys - declared_keys or (session.channels and set(session.channels) - {c["channel_id"] for c in declared}):
        fail("missing_channel_endpoint", "Finalization must include every authorized channel and captured epoch", 409)
    for end in declared:
        received = next((c for c in actual["channels"] if (c["channel_id"], c["capture_epoch"]) == (end["channel_id"], end["capture_epoch"])), None)
        valid_empty = end["last_seq"] == -1 and end["sample_end"] == 0 and not received
        valid = received and received["contiguous_seq"] == end["last_seq"] and received["sample_end"] == end["sample_end"] and not received["gaps"] and max(received["received_seq"]) == end["last_seq"]
        if not valid and not valid_empty:
            complete = False
            issues.append({"channel_id": end["channel_id"], "capture_epoch": end["capture_epoch"], "code": "audio_gap", "message": "Declared endpoint has missing or discontinuous audio"})
    session.final_manifest = declared
    session.status = "COMPLETE" if complete else "INCOMPLETE"
    session.revision += 1
    event(db, session, "session.finalized", {"status": session.status, "issues": issues})
    audit(db, user, "session.finalize", session.id, status=session.status)
    if complete and actual["chunks"] and settings.asr_provider != "dashscope_realtime":
        from .jobs import enqueue
        enqueue(db, user, session, "asr", {"manifest": declared, "source_version": session.source_version})
    db.commit()
    return manifest(db, session) | {"issues": issues}


def read_audio(db, session, channel, epoch, sample_start=0, sample_end=None):
    chunks = db.scalars(select(AudioChunk).where(AudioChunk.session_id == session.id, AudioChunk.channel_id == channel, AudioChunk.capture_epoch == str(epoch)).order_by(AudioChunk.sample_start)).all()
    if not chunks:
        fail("not_found", "Audio channel not found", 404)
    end = sample_end if sample_end is not None else max(c.sample_start + c.sample_count for c in chunks)
    if end <= sample_start or sample_start < 0 or end - sample_start > 16000 * 3600:
        fail("invalid_range", "Audio range is invalid or exceeds one hour")
    cursor = sample_start
    pieces = []
    for chunk in chunks:
        chunk_end = chunk.sample_start + chunk.sample_count
        if chunk_end <= sample_start or chunk.sample_start >= end:
            continue
        if chunk.status != "AVAILABLE" or chunk.expires_at < now():
            fail("audio_expired", "Audio evidence is expired, deleted or quarantined", 410)
        if chunk.sample_start > cursor:
            fail("audio_gap", "Audio evidence has missing samples", 409)
        path = __import__("pathlib").Path(chunk.object_path)
        if not path.is_file():
            fail("audio_unavailable", "Audio object is unavailable", 410)
        try:
            raw = Fernet(settings.audio_key.encode()).decrypt(path.read_bytes())
        except InvalidToken:
            fail("object_corrupt", "Audio integrity verification failed", 503)
        if hashlib.sha256(raw).hexdigest() != chunk.sha256:
            fail("object_corrupt", "Audio integrity verification failed", 503)
        left, right = max(cursor, chunk.sample_start), min(end, chunk_end)
        pieces.append(raw[(left - chunk.sample_start) * 2:(right - chunk.sample_start) * 2])
        cursor = right
    if cursor != end:
        fail("audio_gap", "Requested audio range is incomplete", 409)
    return b"".join(pieces)


def wav_bytes(raw):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(raw)
    return buffer.getvalue()
