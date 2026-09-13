import asyncio
import random
import threading

from sqlalchemy import and_, or_, select, update

from .audio import event, read_audio, wav_bytes
from .clinical import add_transcript, digest
from .config import settings
from . import extraction
from .db import SessionLocal
from .models import CaptureSession, Encounter, Fact, Job, Note, User, now
from .providers import ProviderError, suggest, transcribe
from .security import access_session


def enqueue(db, user, session, kind, payload):
    session = access_session(db, user, session.id, for_update=True)
    llm_job = kind in {"suggestions", "fact_extraction"}
    key = digest({"hospital": session.hospital_id, "session": session.id, "kind": kind, "payload": payload, "model": settings.llm_model if llm_job else settings.asr_model, "provider": settings.model_provider if llm_job else settings.asr_provider, "prompt": extraction.PROMPT_VERSION if kind == "fact_extraction" else "1.0"})
    existing = db.scalar(select(Job).where(Job.input_digest == key))
    if existing:
        return existing
    job = Job(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, kind=kind, input=payload, input_digest=key, actor_id=user.id)
    db.add(job)
    db.flush()
    event(db, session, "job.queued", {"job_id": job.id, "kind": kind})
    return job


def claim_job(kinds=None):
    with SessionLocal() as db:
        eligibility = or_(and_(Job.state.in_(["QUEUED", "RETRY_WAIT"]), Job.next_attempt_at <= now()), and_(Job.state == "RUNNING", Job.lease_until < now()))
        query = select(Job).where(eligibility)
        if kinds:
            query = query.where(Job.kind.in_(kinds))
        job = db.scalar(query.order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        generation = job.generation + 1
        result = db.execute(update(Job).where(Job.id == job.id, Job.generation == job.generation, eligibility).values(state="RUNNING", generation=generation, attempts=job.attempts + 1, lease_until=now() + settings.job_lease_seconds))
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return job.id, generation


def _heartbeat(job_id, generation, stop):
    while not stop.wait(max(1, settings.job_lease_seconds / 4)):
        with SessionLocal() as db:
            result = db.execute(update(Job).where(Job.id == job_id, Job.generation == generation, Job.state == "RUNNING", Job.lease_until >= now()).values(lease_until=now() + settings.job_lease_seconds))
            db.commit()
            if result.rowcount != 1:
                return


def execute_job(job_id, generation):
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(job_id, generation, stop), daemon=True)
    heartbeat.start()
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            user = db.get(User, job.actor_id)
            session = access_session(db, user, job.session_id)
            if job.generation != generation or job.state != "RUNNING":
                return
            kind, payload = job.kind, job.input
            if kind == "asr":
                audio = []
                for channel in payload["manifest"]:
                    if channel["last_seq"] >= 0:
                        raw = read_audio(db, session, channel["channel_id"], channel["capture_epoch"], 0, channel["sample_end"])
                        audio.append((channel, wav_bytes(raw)))
        # Network/model calls run without holding the database transaction.
        if kind == "suggestions":
            result = suggest(payload)
        elif kind == "fact_extraction":
            result = extraction.extract(payload)
        elif kind == "asr":
            result = {"transcripts": [{"channel": channel, **transcribe(wav)} for channel, wav in audio]}
        else:
            raise ProviderError("unknown_job", "Unsupported persistent job type")
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            user = db.get(User, job.actor_id)
            session = access_session(db, user, job.session_id, for_update=True)
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True))
            if job.state != "RUNNING" or job.generation != generation or job.lease_until < now():
                return
            stale = db.get(Encounter, session.encounter_id).source_version != payload.get("source_version")
            if kind == "suggestions":
                note = db.get(Note, payload["note_id"])
                stale = stale or not note or note.revision != payload["base_revision"] or note.status == "QUARANTINED"
                for fid, revision in payload["facts_snapshot"].items():
                    fact = db.get(Fact, fid)
                    stale = stale or not fact or fact.status != "AVAILABLE" or fact.revision != revision
            elif kind == "fact_extraction":
                stale = stale or extraction.snapshot_stale(db, session, payload)
            if stale:
                job.state = "STALE"
                job.result = {"requires_physician_merge": True, "reason": "Input revision changed; stale output was not published"}
            else:
                if kind == "asr":
                    transcript_ids = []
                    for output in result["transcripts"]:
                        channel = output["channel"]
                        evidence = {"channel_id": channel["channel_id"], "capture_epoch": str(channel["capture_epoch"]), "sample_start": 0, "sample_end": channel["sample_end"]}
                        transcript = add_transcript(db, user, session, output["text"], "unknown", "unknown", origin="asr", audio_range=evidence, asr_run_id=job.id + ":" + str(generation))
                        transcript_ids.append(transcript.id)
                    result = {"transcript_ids": transcript_ids, "provider": settings.asr_provider, "model_version": settings.asr_model}
                    follow_up = extraction.enqueue_extraction(db, user, session, transcript_ids=transcript_ids, automatic=True)
                    result["extraction_job_id"] = follow_up.id if follow_up else None
                elif kind == "fact_extraction":
                    result = extraction.publish(db, user, session, job, result)
                job.state = "SUCCEEDED"
                job.result = result
                job.error = None
            event(db, session, "job.finished", {"job_id": job.id, "state": job.state})
            db.commit()
    except Exception as error:
        from fastapi import HTTPException
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if not job:
                return
            session = db.scalar(select(CaptureSession).where(CaptureSession.id == job.session_id).with_for_update())
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True))
            if job.state != "RUNNING" or job.generation != generation or job.lease_until < now():
                return
            transient = isinstance(error, ProviderError) and error.transient
            if isinstance(error, HTTPException):
                code, message = error.detail.get("code", "access_denied"), error.detail.get("message", "Access is no longer valid")
            elif isinstance(error, ProviderError):
                code, message = error.code, str(error)
            else:
                code, message = "internal_job_error", "Job failed before publication; inspect protected operational logs"
            job.error = {"code": code, "message": message, "stage": "execute", "transient": transient}
            job.state = "RETRY_WAIT" if transient and job.attempts < 4 else "FAILED"
            job.next_attempt_at = now() + min(60, 2 ** job.attempts) + random.random()
            event(db, session, "job.finished", {"job_id": job.id, "state": job.state, "error_code": code})
            db.commit()
    finally:
        stop.set()
        heartbeat.join(timeout=2)


async def worker_loop(stop, kinds):
    while not stop.is_set():
        claimed = await asyncio.to_thread(claim_job, kinds)
        if claimed:
            await asyncio.to_thread(execute_job, *claimed)
        else:
            await asyncio.sleep(0.4)
