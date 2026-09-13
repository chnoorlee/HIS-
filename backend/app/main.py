import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audio, clinical, emr, extraction, jobs, lifecycle, schemas
from .config import settings
from .db import SessionLocal, create_schema, get_db
from .models import AppSetting, Audit, CaptureSession, Encounter, Event, Export, Fact, Incident, Job, Note, RemoteDraft, Source, Transcript, User, now
from .security import access_encounter, access_session, audit, check_password, current_user, decode_user, fail, make_token, require_role, user_json
from .seed import seed
from .realtime import manager as realtime_manager


@asynccontextmanager
async def lifespan(app):
    create_schema()
    with SessionLocal() as db:
        try:
            seed(db)
        except IntegrityError:
            db.rollback()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(jobs.worker_loop(stop, kinds)) for kinds in [["asr"], ["suggestions", "fact_extraction"]]] if settings.run_worker else []
    app.state.worker_stop = stop
    await realtime_manager.start()
    try:
        yield
    finally:
        stop.set()
        await realtime_manager.stop()
        if tasks:
            await asyncio.gather(*tasks)


app = FastAPI(title="住院语音病历工作台 API", version="1.0.0", lifespan=lifespan, description="Synthetic development mode is explicitly separated from approved hospital production providers.")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.allowed_origins.split(",")], allow_credentials=False, allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type", "Last-Event-ID"])


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
    return response


def serialized_job(job):
    return clinical.row_json(job) | {"status": job.state}


def scoped_entity(db, user, model, entity_id, quarantined=False):
    row = db.get(model, entity_id)
    if not row:
        fail("not_found", "Resource not found", 404)
    if isinstance(row, Export):
        note = db.get(Note, row.note_id)
        access_session(db, user, note.session_id, allow_quarantined=quarantined)
    else:
        access_session(db, user, row.session_id, allow_quarantined=quarantined)
    return row


@app.get("/health")
@app.get("/api/v1/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "environment": settings.env, "version": "1.0.0", "synthetic": settings.env != "production"}


@app.get("/api/v1/system/status")
def system_status(user=Depends(current_user)):
    return {
        "environment": settings.env,
        "mode": "production" if settings.env == "production" else "synthetic_development",
        "synthetic": settings.env != "production",
        "model_provider": settings.model_provider,
        "llm_ready": settings.model_provider == "demo" or bool(settings.llm_base_url and settings.llm_model),
        "fact_extraction_ready": settings.model_provider == "openai_compatible" and bool(settings.llm_base_url and settings.llm_model and settings.llm_api_key),
        "asr_provider": settings.asr_provider,
        "asr_ready": settings.asr_provider != "unavailable" and bool(settings.asr_base_url and settings.asr_model),
        "asr_message": "未配置语音识别服务；录音可保存，可人工录入。" if settings.asr_provider == "unavailable" else "使用院方配置的识别适配器",
        "emr_provider": settings.emr_provider,
        "emr_capabilities": emr.connector_capabilities(),
        "storage": "encrypted-local-object-store",
        "database": "sqlite-local-development" if settings.database_url.startswith("sqlite") else "postgresql",
        "versions": {"api": "1.0.0", "protocol": "1", "template": "hospital-template-1.0",
                     "prompt": "provenance-organizer-1.0", "extraction_prompt": extraction.PROMPT_VERSION,
                     "dictionary": "unconfigured", "llm_model": settings.llm_model or "synthetic-extractive-demo-1.0",
                     "asr_model": settings.asr_model or "unconfigured"},
    }


_login_attempts = {}


@app.get("/api/v1/auth/config")
def auth_config():
    return {"environment": settings.env, "mode": "oidc" if settings.env == "production" else "local",
            "authority": settings.oidc_issuer, "client_id": settings.oidc_client_id, "scope": settings.oidc_scope}


@app.post("/api/v1/auth/login")
def login(body: schemas.LoginIn, request: Request, db=Depends(get_db)):
    if settings.env == "production":
        fail("local_login_disabled", "Hospital OIDC identity is required", 403)
    ip = request.client.host if request.client else "unknown"
    attempts = [x for x in _login_attempts.get(ip, []) if now() - x < 60]
    if len(attempts) >= 15:
        fail("rate_limited", "Too many login attempts; retry in one minute", 429)
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not user.active or not check_password(body.password, user.password_hash):
        _login_attempts[ip] = attempts + [now()]
        fail("invalid_credentials", "账号或密码不正确。", 401)
    audit(db, user, "auth.login", user.id)
    db.commit()
    return {"access_token": make_token(user), "token_type": "bearer", "expires_in": 28800, "user": user_json(user)}


@app.get("/api/v1/auth/me")
def me(user=Depends(current_user)):
    return user_json(user)


@app.get("/api/v1/encounters")
def encounters(search: str = Query("", max_length=128), db=Depends(get_db), user=Depends(current_user)):
    query = select(Encounter).where(Encounter.hospital_id == user.hospital_id, Encounter.id.in_(user.encounter_ids))
    rows = db.scalars(query.order_by(Encounter.bed)).all()
    return [clinical.row_json(row) for row in rows if not search or search.lower() in (row.patient_name + row.patient_id + row.admission_id + row.bed).lower()]


@app.get("/api/v1/encounters/{encounter_id}")
def encounter(encounter_id: str, db=Depends(get_db), user=Depends(current_user)):
    value = access_encounter(db, user, encounter_id)
    audit(db, user, "encounter.read", encounter_id)
    db.commit()
    return clinical.row_json(value)


@app.get("/api/v1/encounters/{encounter_id}/sources")
def sources(encounter_id: str, db=Depends(get_db), user=Depends(current_user)):
    access_encounter(db, user, encounter_id)
    audit(db, user, "sources.read", encounter_id)
    db.commit()
    return [clinical.row_json(s) for s in db.scalars(select(Source).where(Source.encounter_id == encounter_id, Source.hospital_id == user.hospital_id).order_by(Source.created_at.desc())).all()]


@app.post("/api/v1/encounters/{encounter_id}/sources/refresh")
def refresh_sources(encounter_id: str, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    encounter = access_encounter(db, user, encounter_id)
    if settings.emr_provider == "mock":
        previous = db.scalars(select(Source).where(Source.encounter_id == encounter_id, Source.status == "AVAILABLE")).all()
        refreshed = [{"source_system": s.source_system, "source_key": s.source_key, "title": s.title, "body": s.body, "occurred_at": s.occurred_at, "recorded_at": s.recorded_at} for s in previous]
    else:
        import httpx
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                response = client.get(settings.emr_base_url.rstrip("/") + "/encounters/" + encounter.admission_id + "/sources", headers={"Authorization": "Bearer " + settings.emr_api_key})
                response.raise_for_status()
                external = response.json()
            if external.get("patient_id") != encounter.patient_id or external.get("admission_id") != encounter.admission_id or not isinstance(external.get("sources"), list):
                fail("source_binding_mismatch", "Hospital returned mismatched source identity", 409)
            refreshed = external["sources"]
        except httpx.HTTPError:
            fail("source_unavailable", "Hospital source retrieval failed; absence is not a negative finding", 503)
    access_encounter(db, user, encounter_id)
    encounter.source_version += 1
    db.execute(update(Source).where(Source.encounter_id == encounter_id, Source.status == "AVAILABLE").values(status="CORRECTED"))
    for s in refreshed:
        if not all(key in s for key in ["source_system", "source_key", "title", "body", "occurred_at", "recorded_at"]):
            fail("source_format", "Hospital source is missing required provenance", 502)
        db.add(Source(hospital_id=user.hospital_id, encounter_id=encounter_id, version=encounter.source_version, **{k: s[k] for k in ["source_system", "source_key", "title", "body", "occurred_at", "recorded_at"]}))
    for session in db.scalars(select(CaptureSession).where(CaptureSession.encounter_id == encounter_id)):
        clinical.invalidate(db, session.id, reason="Hospital source snapshot refreshed")
        audio.event(db, session, "sources.changed", {"source_version": encounter.source_version})
    audit(db, user, "sources.refresh", encounter_id, source_version=encounter.source_version)
    db.commit()
    return sources(encounter_id, db, user)


@app.post("/api/v1/sessions", status_code=201)
def create_session(body: schemas.SessionIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    encounter = access_encounter(db, user, body.encounter_id)
    if settings.env == "production" and not body.consent_record:
        fail("policy_record_required", "Hospital authorization and notice policy record is required")
    session = CaptureSession(hospital_id=user.hospital_id, encounter_id=encounter.id, patient_id=encounter.patient_id, operator_id=user.id, mode=body.mode, source_version=encounter.source_version, consent_record=body.consent_record or "synthetic-development")
    db.add(session)
    db.flush()
    audio.event(db, session, "session.created")
    audit(db, user, "session.create", session.id)
    db.commit()
    return clinical.row_json(session)


@app.get("/api/v1/sessions")
def sessions(encounter_id: str | None = None, db=Depends(get_db), user=Depends(current_user)):
    if encounter_id:
        access_encounter(db, user, encounter_id)
    query = select(CaptureSession).where(CaptureSession.hospital_id == user.hospital_id, CaptureSession.encounter_id.in_(user.encounter_ids))
    if encounter_id:
        query = query.where(CaptureSession.encounter_id == encounter_id)
    return [clinical.row_json(s) for s in db.scalars(query.order_by(CaptureSession.created_at.desc())).all()]


@app.get("/api/v1/sessions/{session_id}")
def session_details(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    return clinical.row_json(access_session(db, user, session_id))


@app.post("/api/v1/sessions/{session_id}/pause")
def pause(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    session = access_session(db, user, session_id, for_update=True)
    if session.status not in {"CREATED", "RECORDING", "PAUSED"}:
        fail("invalid_state", "Session cannot pause in its current state", 409)
    session.status, session.revision = "PAUSED", session.revision + 1
    audio.event(db, session, "session.paused")
    db.commit()
    return clinical.row_json(session)


@app.post("/api/v1/sessions/{session_id}/resume")
def resume(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    session = access_session(db, user, session_id, for_update=True)
    if session.status not in {"PAUSED", "CREATED", "RECORDING"} or session.expires_at < now():
        fail("invalid_state", "Session cannot resume or authorization expired", 409)
    session.status, session.revision = "RECORDING", session.revision + 1
    audio.event(db, session, "session.resumed")
    db.commit()
    return clinical.row_json(session)


@app.post("/api/v1/sessions/{session_id}/finalize")
def finalize(session_id: str, body: schemas.FinalizeIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    session = access_session(db, user, session_id)
    return audio.finalize(db, user, session, body)


@app.post("/api/v1/sessions/{session_id}/quarantine")
def quarantine(session_id: str, body: schemas.ReasonIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer", "admin")
    session = access_session(db, user, session_id, allow_quarantined=True)
    incident = clinical.quarantine(db, user, session, body.reason)
    db.commit()
    return clinical.row_json(incident)


@app.post("/api/v1/sessions/{session_id}/stream-ticket")
def stream_ticket(session_id: str, body: schemas.TicketIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    return audio.issue_ticket(db, user, access_session(db, user, session_id), body)


@app.websocket("/api/v1/audio")
async def audio_socket(websocket: WebSocket, ticket: str = ""):
    origin = websocket.headers.get("origin")
    if origin and origin not in [o.strip() for o in settings.allowed_origins.split(",")]:
        await websocket.close(code=4403)
        return
    try:
        with SessionLocal() as db:
            stream = audio.consume_ticket(db, ticket)
            user = db.get(User, stream.user_id)
            require_role(user, "doctor", "reviewer")
            session = access_session(db, user, stream.session_id)
            if session.generation != stream.generation:
                fail("stale_connection", "A newer stream generation exists", 409)
    except HTTPException as error:
        await websocket.close(code=4401 if error.status_code == 401 else 4403)
        return
    await websocket.accept()
    window, received = time.monotonic(), 0
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 460000:
                fail("chunk_too_large", "Audio message exceeds the protocol size limit", 413)
            if time.monotonic() - window > 1:
                window, received = time.monotonic(), 0
            received += 1
            if received > 80:
                fail("rate_limited", "Audio stream exceeds accepted chunk rate", 429)
            try:
                body = schemas.ChunkIn.model_validate_json(raw)
                def persist():
                    with SessionLocal() as db:
                        current = db.get(User, stream.user_id)
                        require_role(current, "doctor", "reviewer")
                        return audio.store_chunk(db, current, stream, body)
                ack = await asyncio.to_thread(persist)
                await websocket.send_json(ack)
                await realtime_manager.consume_durable(stream.session_id, stream.user_id)
            except ValidationError:
                await websocket.send_json({"type": "ERROR", "code": "invalid_chunk", "message": "Chunk fields or audio format are invalid"})
            except HTTPException as error:
                await websocket.send_json({"type": "ERROR", **error.detail})
                if error.status_code in {401, 403, 423} or error.detail["code"] in {"stale_connection", "chunk_conflict"}:
                    await websocket.close(code=4403)
                    return
    except WebSocketDisconnect:
        pass
    except HTTPException as error:
        await websocket.send_json({"type": "ERROR", **error.detail})
        await websocket.close(code=4400)


@app.get("/api/v1/sessions/{session_id}/audio-manifest")
def audio_manifest(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    return audio.manifest(db, access_session(db, user, session_id))


@app.post("/api/v1/sessions/{session_id}/audio-gaps")
def audio_gaps(session_id: str, body: schemas.AudioGapsIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    session = access_session(db, user, session_id, for_update=True)
    accepted = []
    for gap in body.gaps:
        if gap.session_id and gap.session_id != session.id:
            fail("binding_mismatch", "Gap ledger belongs to a different capture session", 403)
        if session.channels and gap.channel_id not in session.channels:
            fail("channel_forbidden", "Gap channel is not authorized", 403)
        value = gap.model_dump() | {"capture_epoch": str(gap.capture_epoch), "session_id": session.id}
        key = clinical.digest({k: value[k] for k in ["session_id", "channel_id", "capture_epoch", "seq", "sample_start", "sample_count", "reason"]})
        existing = db.get(Event, key)
        if existing and (existing.session_id != session.id or existing.hospital_id != user.hospital_id):
            fail("ledger_conflict", "Gap event scope mismatch", 409)
        if not existing:
            audio.event(db, session, "audio.gap_reported", value, event_id=key)
            db.add(Incident(hospital_id=user.hospital_id, encounter_id=session.encounter_id, session_id=session.id, reason="Capture client reported missing audio: " + gap.reason, dependencies=value))
        accepted.append({"channel_id": gap.channel_id, "capture_epoch": str(gap.capture_epoch), "seq": gap.seq})
    if session.status != "COMPLETE":
        session.status = "INCOMPLETE"
    audit(db, user, "audio.gap_report", session.id, count=len(accepted))
    db.commit()
    return {"status": "ACK_DURABLE", "session_id": session.id, "accepted": accepted}


@app.get("/api/v1/sessions/{session_id}/audio")
def playback(session_id: str, channel_id: str, capture_epoch: str, sample_start: int = 0, sample_end: int | None = None, db=Depends(get_db), user=Depends(current_user)):
    session = access_session(db, user, session_id)
    raw = audio.read_audio(db, session, channel_id, capture_epoch, sample_start, sample_end)
    audit(db, user, "audio.playback", session.id, channel=channel_id, epoch=capture_epoch, sample_start=sample_start, sample_end=sample_end)
    db.commit()
    return Response(audio.wav_bytes(raw), media_type="audio/wav")


@app.get("/api/v1/sessions/{session_id}/transcripts")
def transcripts(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    access_session(db, user, session_id)
    audit(db, user, "transcripts.read", session_id)
    db.commit()
    return [clinical.row_json(t) for t in db.scalars(select(Transcript).where(Transcript.session_id == session_id, Transcript.status == "AVAILABLE").order_by(Transcript.created_at)).all()]


@app.post("/api/v1/sessions/{session_id}/transcripts", status_code=201)
def add_transcript(session_id: str, body: schemas.TranscriptIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    session = access_session(db, user, session_id)
    transcript = clinical.add_transcript(db, user, session, body.text, body.speaker, body.subject, body.section)
    db.commit()
    return clinical.row_json(transcript)


@app.patch("/api/v1/transcripts/{transcript_id}")
def correct_transcript(transcript_id: str, body: schemas.TranscriptPatch, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    return clinical.patch_transcript(db, user, scoped_entity(db, user, Transcript, transcript_id), body)


@app.post("/api/v1/sessions/{session_id}/demo-script")
def demo_script(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    if settings.env == "production":
        fail("demo_disabled", "Synthetic fixtures are disabled in production", 403)
    session = access_session(db, user, session_id)
    if not db.get(Encounter, session.encounter_id).synthetic:
        fail("demo_scope", "Demonstration scripts require a synthetic encounter", 403)
    if any(t.body.get("origin") == "synthetic_script" for t in db.scalars(select(Transcript).where(Transcript.session_id == session_id))):
        fail("demo_already_loaded", "This synthetic script has already been loaded", 409)
    for section, speaker, subject, text in clinical.DEMO_SCRIPT:
        clinical.add_transcript(db, user, session, text, speaker, subject, section, origin="synthetic_script")
    audit(db, user, "demo.script", session.id)
    db.commit()
    return {"synthetic": True, "count": len(clinical.DEMO_SCRIPT), "message": "已载入虚构脚本；这不是语音识别结果。"}


@app.get("/api/v1/sessions/{session_id}/facts")
def facts(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    access_session(db, user, session_id)
    return [clinical.row_json(f) for f in clinical.session_facts(db, session_id)]


@app.post("/api/v1/sessions/{session_id}/extract-facts", status_code=202)
def extract_facts(session_id: str, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    session = access_session(db, user, session_id, for_update=True)
    job = extraction.enqueue_extraction(db, user, session)
    db.commit()
    return serialized_job(job)


@app.patch("/api/v1/facts/{fact_id}")
def correct_fact(fact_id: str, body: schemas.FactPatch, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    return clinical.patch_fact(db, user, scoped_entity(db, user, Fact, fact_id), body)


@app.post("/api/v1/notes", status_code=201)
def create_note(body: schemas.NoteIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    return clinical.create_note(db, user, access_session(db, user, body.session_id), body.note_type)


@app.get("/api/v1/notes")
def notes(encounter_id: str | None = None, db=Depends(get_db), user=Depends(current_user)):
    if encounter_id:
        access_encounter(db, user, encounter_id)
    query = select(Note).where(Note.hospital_id == user.hospital_id, Note.encounter_id.in_(user.encounter_ids), Note.status != "QUARANTINED")
    if encounter_id:
        query = query.where(Note.encounter_id == encounter_id)
    return [clinical.note_json(db, n) for n in db.scalars(query.order_by(Note.created_at.desc())).all()]


@app.get("/api/v1/notes/{note_id}")
def note_details(note_id: str, db=Depends(get_db), user=Depends(current_user)):
    note = scoped_entity(db, user, Note, note_id)
    audit(db, user, "note.read", note.id)
    db.commit()
    return clinical.note_json(db, note)


@app.patch("/api/v1/notes/{note_id}")
def edit_note(note_id: str, body: schemas.NotePatch, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    return clinical.patch_note(db, user, scoped_entity(db, user, Note, note_id), body)


@app.post("/api/v1/notes/{note_id}/suggestions", status_code=202)
def suggestions(note_id: str, body: schemas.SuggestionIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    note = scoped_entity(db, user, Note, note_id)
    if note.revision != body.base_revision:
        fail("revision_conflict", "Suggestion base revision is no longer current", 409)
    titles = dict(clinical.TEMPLATES[note.note_type])
    sections = body.sections or list(titles)
    if set(sections) - set(titles):
        fail("invalid_sections", "Unknown template section requested")
    material = clinical.session_facts(db, note.session_id)
    payload = {"note_id": note.id, "base_revision": note.revision, "sections": sections, "titles": titles, "source_version": db.get(Encounter, note.encounter_id).source_version, "facts_snapshot": {f.id: f.revision for f in material}, "facts": [clinical.row_json(f) for f in material]}
    job = jobs.enqueue(db, user, db.get(CaptureSession, note.session_id), "suggestions", payload)
    db.commit()
    return serialized_job(job)


@app.post("/api/v1/notes/{note_id}/review")
def review(note_id: str, body: schemas.ReviewIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    return clinical.review_note(db, user, scoped_entity(db, user, Note, note_id), body)


@app.get("/api/v1/jobs")
def list_jobs(encounter_id: str | None = None, db=Depends(get_db), user=Depends(current_user)):
    if encounter_id:
        access_encounter(db, user, encounter_id)
    query = select(Job).where(Job.hospital_id == user.hospital_id, Job.encounter_id.in_(user.encounter_ids))
    if encounter_id:
        query = query.where(Job.encounter_id == encounter_id)
    return [serialized_job(j) for j in db.scalars(query.order_by(Job.created_at.desc()).limit(100)).all() if db.get(CaptureSession, j.session_id).status not in {"QUARANTINED", "DELETED"}]


@app.get("/api/v1/jobs/{job_id}")
def job_details(job_id: str, db=Depends(get_db), user=Depends(current_user)):
    return serialized_job(scoped_entity(db, user, Job, job_id))


@app.post("/api/v1/notes/{note_id}/exports", status_code=202)
def export_note(note_id: str, body: schemas.ExportIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer")
    operation = emr.prepare_export(db, user, scoped_entity(db, user, Note, note_id), body)
    emr.send_export(operation.id)
    db.refresh(operation)
    return clinical.row_json(operation)


@app.get("/api/v1/exports/{export_id}")
def export_details(export_id: str, db=Depends(get_db), user=Depends(current_user)):
    return clinical.row_json(scoped_entity(db, user, Export, export_id))


@app.get("/api/v1/exports")
def list_exports(encounter_id: str | None = None, db=Depends(get_db), user=Depends(current_user)):
    if encounter_id:
        access_encounter(db, user, encounter_id)
    query = select(Export).where(Export.hospital_id == user.hospital_id, Export.encounter_id.in_(user.encounter_ids))
    if encounter_id:
        query = query.where(Export.encounter_id == encounter_id)
    result = []
    for operation in db.scalars(query.order_by(Export.created_at.desc()).limit(200)):
        value = clinical.row_json(operation)
        value.pop("payload", None)
        result.append(value)
    return result


@app.post("/api/v1/exports/{export_id}/reconcile")
def reconcile(export_id: str, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "doctor", "reviewer", "admin")
    operation = scoped_entity(db, user, Export, export_id, quarantined=True)
    emr.reconcile_export(operation.id)
    db.refresh(operation)
    audit(db, user, "export.reconcile", operation.id, status=operation.status)
    db.commit()
    result = clinical.row_json(operation)
    if db.get(CaptureSession, db.get(Note, operation.note_id).session_id).status in {"QUARANTINED", "DELETED"}:
        result.pop("payload", None)
    return result


def event_batch(db, hospital_id, session_id, stamp, cursor):
    boundary = or_(Event.created_at > stamp, and_(Event.created_at == stamp, Event.id > (cursor or "")))
    return db.scalars(select(Event).where(Event.session_id == session_id, Event.hospital_id == hospital_id, boundary)
                      .order_by(Event.created_at, Event.id).limit(100)).all()


@app.get("/api/v1/sessions/{session_id}/events")
async def events(session_id: str, request: Request, after: str | None = None, user=Depends(current_user)):
    cursor = after or request.headers.get("last-event-id")
    with SessionLocal() as db:
        current = db.get(User, user.id)
        access_session(db, current, session_id)
        stamp = 0
        if cursor:
            prior = db.get(Event, cursor)
            if not prior or prior.session_id != session_id or prior.hospital_id != user.hospital_id:
                fail("event_cursor_forbidden", "Event cursor belongs to a different session or is unavailable", 403)
            stamp = prior.created_at
    async def generate():
        nonlocal stamp, cursor
        while not await request.is_disconnected():
            with SessionLocal() as db:
                try:
                    current = decode_user(request.headers.get("authorization", "").removeprefix("Bearer "), db)
                    session = access_session(db, current, session_id, allow_quarantined=True)
                except HTTPException:
                    yield "event: access.revoked\ndata: {\"status\":\"REVOKED\"}\n\n"
                    return
                if session.status in {"QUARANTINED", "DELETED"}:
                    yield "event: session.quarantined\ndata: " + json.dumps({"status": session.status}) + "\n\n"
                    return
                rows = event_batch(db, user.hospital_id, session_id, stamp, cursor)
                for row in rows:
                    stamp, cursor = row.created_at, row.id
                    yield f"id: {row.id}\nevent: {row.kind}\ndata: " + json.dumps({"type": row.kind, **row.payload}, ensure_ascii=False) + "\n\n"
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.get("/api/v1/audit")
def audit_events(limit: int = Query(100, ge=1, le=500), db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin", "reviewer")
    return [clinical.row_json(a) for a in db.scalars(select(Audit).where(Audit.hospital_id == user.hospital_id).order_by(Audit.created_at.desc()).limit(limit))]


@app.get("/api/v1/admin/settings")
def get_settings(db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    return emr.hospital_settings(db, user.hospital_id) | {"environment": settings.env, "model_provider": settings.model_provider, "asr_provider": settings.asr_provider, "emr_provider": settings.emr_provider, "versions": system_status(user)["versions"]}


@app.patch("/api/v1/admin/settings")
def edit_settings(body: dict, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    allowed = {"export_enabled", "mock_emr_scenario", "audio_retention_hours", "content_retention_days", "recovery_isolation"}
    if set(body) - allowed:
        fail("invalid_settings", "Unsupported settings or secret fields")
    for key in {"export_enabled", "recovery_isolation"} & body.keys():
        if not isinstance(body[key], bool):
            fail("invalid_settings", "Feature switches must be boolean")
    for key, maximum in [("audio_retention_hours", 168), ("content_retention_days", 90)]:
        if key in body and (type(body[key]) is not int or not 1 <= body[key] <= maximum):
            fail("invalid_settings", "Retention setting exceeds accepted policy range")
    if "mock_emr_scenario" in body:
        if settings.env == "production" or body["mock_emr_scenario"] not in {"normal", "timeout_after_commit", "timeout_before_commit", "query_not_found", "reject", "signed_conflict"}:
            fail("invalid_scenario", "Mock failure scenario is invalid or disabled")
    row = db.scalar(select(AppSetting).where(AppSetting.key == user.hospital_id).with_for_update().execution_options(populate_existing=True))
    if not row:
        fail("hospital_not_initialized", "Hospital settings must be provisioned before administrative updates", 409)
    if body.get("recovery_isolation") is False and emr.hospital_settings(db, user.hospital_id).get("recovery_isolation"):
        fail("recovery_verification_required", "恢复隔离需先完成删除清单、权限和 EMR 恢复窗口核对，不能直接关闭。", 409)
    row.value = row.value | body
    audit(db, user, "admin.settings", user.hospital_id, changed_fields=list(body))
    db.commit()
    return get_settings(db, user)


@app.get("/api/v1/admin/incidents")
def incidents(db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin", "reviewer")
    return [clinical.row_json(i) for i in db.scalars(select(Incident).where(Incident.hospital_id == user.hospital_id).order_by(Incident.created_at.desc()))]


@app.post("/api/v1/admin/retention/run")
def retention(db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    return lifecycle.retention_sweep(db, user)


@app.post("/api/v1/admin/sessions/{session_id}/delete")
def delete_session(session_id: str, body: schemas.ReasonIn, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    return lifecycle.purge_session(db, user, access_session(db, user, session_id, allow_quarantined=True), body.reason)


@app.get("/api/v1/admin/deletion-ledger")
def deletion_ledger(db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    return [clinical.row_json(i) for i in db.scalars(select(Incident).where(Incident.hospital_id == user.hospital_id, Incident.status.in_(["LOCAL_DELETED", "OPEN"]))).all()]


@app.patch("/api/v1/admin/users/{user_id}/grants")
def grants(user_id: str, body: dict, db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    target = db.get(User, user_id)
    if not target or target.hospital_id != user.hospital_id:
        fail("not_found", "Hospital identity not found", 404)
    if set(body) - {"encounter_ids", "active"}:
        fail("invalid_grants", "Only encounter grants and active state can be changed")
    if "encounter_ids" in body:
        ids = body["encounter_ids"]
        if not isinstance(ids, list) or any(not isinstance(i, str) or not db.get(Encounter, i) or db.get(Encounter, i).hospital_id != user.hospital_id for i in ids):
            fail("invalid_grants", "Encounter grants must belong to this hospital")
        target.encounter_ids = list(set(ids))
    if "active" in body:
        if not isinstance(body["active"], bool):
            fail("invalid_grants", "Active state must be boolean")
        target.active = body["active"]
    target.auth_version += 1
    audit(db, user, "admin.grants", target.id)
    db.commit()
    return user_json(target)


@app.get("/api/v1/admin/users")
def users(db=Depends(get_db), user=Depends(current_user)):
    require_role(user, "admin")
    return [user_json(u) | {"active": u.active} for u in db.scalars(select(User).where(User.hospital_id == user.hospital_id))]
