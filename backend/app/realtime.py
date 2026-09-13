"""Persistent live-ASR scheduling; wakeups and active sockets are disposable."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import HTTPException
from sqlalchemy import select, update

from integrations.asr import ASRError, AudioBlock, DashscopeConfig, DashscopeRealtime

from .audio import event, read_audio
from .clinical import add_transcript, invalidate
from .config import settings
from .db import SessionLocal
from .extraction import enqueue_extraction
from .jobs import claim_job, enqueue
from .models import AudioChunk, CaptureSession, Fact, FactRevision, Job, Transcript, TranscriptRevision, User, now
from .security import access_session


class RealtimeManager:
    def __init__(self):
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task = None
        self._active: dict[str, asyncio.Task] = {}

    async def start(self):
        if settings.asr_provider != "dashscope_realtime" or self._loop_task:
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopping = True
        self._wake.set()
        if self._loop_task:
            await self._loop_task
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._loop_task = None

    async def consume_durable(self, session_id: str, user_id: str):
        # Identity and data are read afresh from the database, not trusted from this hint.
        self._wake.set()

    async def _loop(self):
        while not self._stopping:
            try:
                self._schedule()
                while len(self._active) < 20:
                    claimed = claim_job(kinds=["asr_live"])
                    if not claimed:
                        break
                    job_id, generation = claimed
                    task = asyncio.create_task(self._execute(job_id, generation))
                    self._active[job_id] = task
                    task.add_done_callback(lambda completed, identifier=job_id: self._active.pop(identifier, None))
            except Exception:
                # A temporary database outage must not destroy the scheduler. No raw
                # exception is logged because driver messages can contain connection data.
                import logging
                logging.getLogger(__name__).error("Realtime scheduler database operation failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 0.5)
            except TimeoutError:
                pass

    def _schedule(self):
        with SessionLocal() as db:
            sessions = db.scalars(select(CaptureSession).where(
                CaptureSession.status.in_(["RECORDING", "FINALIZING", "COMPLETE", "INCOMPLETE", "PAUSED"])
            )).all()
            for session in sessions:
                user = db.get(User, session.operator_id)
                if not user:
                    continue
                try:
                    access_session(db, user, session.id)
                except HTTPException:
                    continue
                chunks = db.scalars(select(AudioChunk).where(AudioChunk.session_id == session.id,
                                                              AudioChunk.status == "AVAILABLE",
                                                              AudioChunk.expires_at > now())
                                    .order_by(AudioChunk.seq)).all()
                groups: dict[tuple[str, str], list[AudioChunk]] = {}
                for chunk in chunks:
                    groups.setdefault((chunk.channel_id, chunk.capture_epoch), []).append(chunk)
                jobs = db.scalars(select(Job).where(Job.session_id == session.id, Job.kind == "asr_live")
                                  .order_by(Job.created_at)).all()
                for (channel, epoch), available in groups.items():
                    previous = [job for job in jobs if job.input["channel_id"] == channel and job.input["capture_epoch"] == epoch]
                    if any(job.state not in {"SUCCEEDED", "CANCELLED", "STALE"} for job in previous):
                        continue
                    completed = [job for job in previous if job.state == "SUCCEEDED"]
                    last_seq = max((job.result.get("last_seq", -1) for job in completed), default=-1)
                    first = next((chunk for chunk in available if chunk.seq == last_seq + 1), None)
                    if not first:
                        continue
                    enqueue(db, user, session, "asr_live", {"channel_id": channel, "capture_epoch": epoch,
                                                           "start_seq": first.seq, "start_sample": first.sample_start})
            db.commit()

    def _owned(self, db, job_id, generation, *, lock=False):
        snapshot = db.get(Job, job_id)
        if not snapshot:
            raise ASRError("job_lease_lost")
        user = db.get(User, snapshot.actor_id)
        if not user:
            raise ASRError("authorization_revoked")
        session = access_session(db, user, snapshot.session_id, for_update=lock)
        query = select(Job).where(Job.id == job_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        job = db.scalar(query)
        if not job or job.state != "RUNNING" or job.generation != generation or job.lease_until < now():
            raise ASRError("job_lease_lost")
        return job, user, session

    async def _heartbeat(self, job_id, generation):
        while True:
            await asyncio.sleep(max(1, settings.job_lease_seconds / 4))
            with SessionLocal() as db:
                job, _, _ = self._owned(db, job_id, generation)
                db.execute(update(Job).where(Job.id == job.id, Job.generation == generation,
                                             Job.state == "RUNNING", Job.lease_until >= now())
                           .values(lease_until=now() + settings.job_lease_seconds))
                db.commit()

    async def _blocks(self, job_id, generation, cursor):
        idle_since = None
        while True:
            block = None
            with SessionLocal() as db:
                job, _, session = self._owned(db, job_id, generation)
                channel, epoch = job.input["channel_id"], job.input["capture_epoch"]
                chunk = db.scalar(select(AudioChunk).where(AudioChunk.session_id == session.id,
                                                           AudioChunk.channel_id == channel,
                                                           AudioChunk.capture_epoch == epoch,
                                                           AudioChunk.seq == cursor["last_seq"] + 1))
                if chunk:
                    if chunk.sample_start != cursor["sample_end"]:
                        raise ASRError("audio_sample_discontinuity")
                    raw = read_audio(db, session, channel, epoch, chunk.sample_start, chunk.sample_start + chunk.sample_count)
                    block = AudioBlock(channel, epoch, chunk.seq, chunk.sample_start, chunk.sample_count, raw, chunk.sha256)
                    cursor.update(last_seq=chunk.seq, sample_end=chunk.sample_start + chunk.sample_count)
                    idle_since = None
                elif session.status in {"COMPLETE", "PAUSED", "INCOMPLETE"}:
                    idle_since = idle_since or now()
                    if now() - idle_since >= 2:
                        return
                elif session.expires_at < now():
                    return
            if block:
                yield block
            else:
                await asyncio.sleep(0.1)

    def _publish(self, job_id, generation, result):
        with SessionLocal() as db:
            job, user, session = self._owned(db, job_id, generation, lock=True)
            if result.status == "partial":
                event(db, session, "asr.partial", asdict(result))
                db.commit()
                return
            evidence = {"channel_id": result.channel_id, "capture_epoch": result.capture_epoch,
                        "sample_start": result.sample_start, "sample_end": result.sample_end}
            prior = db.scalars(select(Transcript).where(Transcript.session_id == session.id,
                                                        Transcript.status == "AVAILABLE")).all()
            overlaps = []
            for transcript in prior:
                source = transcript.body.get("audio_range") or {}
                if (source.get("channel_id"), source.get("capture_epoch")) != (result.channel_id, result.capture_epoch):
                    continue
                if source == evidence and transcript.body.get("text") == result.text:
                    return
                if max(source.get("sample_start", 0), result.sample_start) < min(source.get("sample_end", 0), result.sample_end):
                    overlaps.append(transcript)
            if overlaps:
                affected = []
                for transcript in overlaps:
                    for fact in db.scalars(select(Fact).where(Fact.session_id == session.id)):
                        if transcript.id not in fact.body.get("source_ids", []):
                            continue
                        # Preserve corrected/excluded text; only its verification state changes.
                        if fact.body.get("confirmation_status") != "excluded":
                            fact.body = fact.body | {"source_changed": True, "confirmation_status": "unconfirmed"}
                            fact.revision += 1
                            db.add(FactRevision(fact_id=fact.id, revision=fact.revision, body=fact.body, actor_id=user.id))
                            affected.append(fact.id)
                if affected:
                    invalidate(db, session.id, affected, "Replayed ASR conflicts with existing source audio")
                event(db, session, "asr.reconciliation_required", {"transcript_ids": [t.id for t in overlaps],
                                                                  "proposal": asdict(result)})
            else:
                transcript = add_transcript(db, user, session, result.text, "unknown", "unknown",
                                            origin="asr", audio_range=evidence, asr_run_id=result.run_id)
                transcript.body = transcript.body | {"provider_segment_id": result.segment_id,
                                                      "provider_revision": result.revision, "stability": "final",
                                                      "provider": result.provider, "model": result.model}
                db.flush()
                revision = db.scalar(select(TranscriptRevision).where(
                    TranscriptRevision.transcript_id == transcript.id, TranscriptRevision.revision == 1))
                revision.body = transcript.body
                enqueue_extraction(db, user, session, transcript_ids=[transcript.id], automatic=True)
                event(db, session, "asr.final", {"transcript_id": transcript.id, **asdict(result)})
            db.commit()

    async def _execute(self, job_id, generation):
        heartbeat = asyncio.create_task(self._heartbeat(job_id, generation))
        try:
            with SessionLocal() as db:
                job, _, _ = self._owned(db, job_id, generation)
                cursor = {"last_seq": job.input["start_seq"] - 1, "sample_end": job.input["start_sample"]}
            adapter = DashscopeRealtime(DashscopeConfig(
                endpoint=settings.asr_base_url, api_key=settings.asr_api_key,
                approved_hosts=frozenset(host.strip() for host in settings.approved_model_hosts.split(",") if host.strip()),
                model=settings.asr_model or "paraformer-realtime-v2",
                allow_loopback_test=settings.env == "test",
            ))
            async for result in adapter.recognize(self._blocks(job_id, generation, cursor)):
                if heartbeat.done():
                    await heartbeat
                self._publish(job_id, generation, result)
            with SessionLocal() as db:
                job, _, session = self._owned(db, job_id, generation, lock=True)
                job.state, job.result = "SUCCEEDED", cursor
                event(db, session, "asr.completed", {"job_id": job_id, **cursor})
                db.commit()
        except asyncio.CancelledError:
            # The lease remains recoverable; shutdown cannot claim that inference finished.
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ASRError) else ("authorization_revoked" if isinstance(exc, HTTPException) else "asr_failed")
            retry = isinstance(exc, ASRError) and exc.retryable
            with SessionLocal() as db:
                snapshot = db.get(Job, job_id)
                if not snapshot:
                    return
                session = db.scalar(select(CaptureSession).where(CaptureSession.id == snapshot.session_id).with_for_update())
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if job and job.state == "RUNNING" and job.generation == generation and job.lease_until >= now():
                    job.state = "RETRY_WAIT" if retry and job.attempts < 4 else "FAILED"
                    job.next_attempt_at = now() + min(30, 2 ** job.attempts)
                    job.error = {"code": code, "message": "实时识别未完成，录音保留策略继续生效。"}
                    if session and session.status not in {"QUARANTINED", "DELETED"}:
                        event(db, session, "asr.failed", {"job_id": job_id, "code": code})
                    db.commit()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


manager = RealtimeManager()
