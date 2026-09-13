import asyncio
from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import SessionLocal
from app.jobs import claim_job
from app.main import event_batch
from app.models import CaptureSession, Event, Fact, Job, Transcript, TranscriptRevision, User, now
from app.realtime import RealtimeManager
from integrations.asr import ASRError, ASRUpdate
from test_protocol import chunk, ticket


def live_job(client, doctor, session):
    token = ticket(client, doctor, session)
    with client.websocket_connect('/api/v1/audio?ticket=' + token) as ws:
        for seq in range(2):
            ws.send_json(chunk(session, seq))
            assert ws.receive_json()['type'] == 'ACK_DURABLE'
    manager = RealtimeManager()
    manager._schedule()
    claimed = claim_job(['asr_live'])
    assert claimed
    result = ASRUpdate('run-1', 'sentence-1', 1, 'doctor_mic', 'epoch-test', 0, 1600,
                       '患者表示既往病史记不清。', 'final', model='provider-test')
    return manager, claimed, result


def test_live_final_is_durable_versioned_and_idempotent(client, doctor, session):
    manager, claimed, result = live_job(client, doctor, session)
    manager._publish(*claimed, replace(result, status='partial', sample_end=None))
    with SessionLocal() as db:
        assert db.query(Transcript).count() == 0
        assert db.query(Fact).count() == 0
    manager._publish(*claimed, result)
    manager._publish(*claimed, result)
    with SessionLocal() as db:
        assert db.query(Transcript).count() == 1
        transcript = db.scalar(select(Transcript))
        revision = db.scalar(select(TranscriptRevision))
        assert transcript.body == revision.body
        assert transcript.body['provider_segment_id'] == 'sentence-1'
        assert transcript.body['stability'] == 'final'
        fact = db.scalar(select(Fact))
        assert fact.body['confirmation_status'] == 'unconfirmed'
        assert fact.body['subject'] == 'unknown'
        assert fact.body['evidence'][0]['audio_range']['sample_end'] == 1600
        assert db.query(Event).filter(Event.kind == 'asr.final').count() == 1


def test_repeated_words_at_different_audio_ranges_are_not_deduplicated(client, doctor, session):
    manager, claimed, result = live_job(client, doctor, session)
    manager._publish(*claimed, result)
    manager._publish(*claimed, replace(result, segment_id='sentence-2', sample_start=1600, sample_end=3200))
    with SessionLocal() as db:
        assert db.query(Transcript).count() == 2


def test_replay_conflict_preserves_physician_text_and_exclusion(client, doctor, session):
    manager, claimed, result = live_job(client, doctor, session)
    manager._publish(*claimed, result)
    with SessionLocal() as db:
        fact = db.scalar(select(Fact))
        fid = fact.id
        fact.body = fact.body | {'text': '医生已经排除此项', 'confirmation_status': 'excluded'}
        db.commit()
    manager._publish(*claimed, replace(result, run_id='run-2', text='另一个识别结果'))
    with SessionLocal() as db:
        assert db.query(Transcript).count() == 1
        assert db.get(Fact, fid).body['text'] == '医生已经排除此项'
        assert db.get(Fact, fid).body['confirmation_status'] == 'excluded'
        assert db.query(Event).filter(Event.kind == 'asr.reconciliation_required').count() == 1


@pytest.mark.parametrize('fence', ['generation', 'lease', 'quarantine', 'permission'])
def test_live_publication_rechecks_fences(client, doctor, session, fence):
    manager, claimed, result = live_job(client, doctor, session)
    with SessionLocal() as db:
        job = db.get(Job, claimed[0])
        if fence == 'generation':
            job.generation += 1
        elif fence == 'lease':
            job.lease_until = now() - 1
        elif fence == 'quarantine':
            db.get(CaptureSession, session['id']).status = 'QUARANTINED'
        else:
            db.get(User, job.actor_id).active = False
        db.commit()
    with pytest.raises((ASRError, HTTPException)):
        manager._publish(*claimed, result)
    with SessionLocal() as db:
        assert db.query(Transcript).count() == 0


def test_live_reader_decrypts_only_durable_contiguous_samples(client, doctor, session):
    manager, claimed, _ = live_job(client, doctor, session)
    async def read():
        cursor = {'last_seq': -1, 'sample_end': 0}
        iterator = manager._blocks(*claimed, cursor)
        first, second = await anext(iterator), await anext(iterator)
        await iterator.aclose()
        assert first.pcm == b'\x01\x00' * 1600
        assert (first.sample_start, second.sample_start) == (0, 1600)
        assert cursor == {'last_seq': 1, 'sample_end': 3200}
    asyncio.run(read())


def test_scheduler_does_not_duplicate_an_active_stream(client, doctor, session):
    manager, _, _ = live_job(client, doctor, session)
    for _ in range(3):
        manager._schedule()
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.kind == 'asr_live').count() == 1


def test_event_replay_keeps_timestamp_ties_across_batches(client, doctor, session):
    with SessionLocal() as db:
        current = db.get(CaptureSession, session['id'])
        for number in range(205):
            db.add(Event(id=f'tied-{number:03}', hospital_id=current.hospital_id,
                         encounter_id=current.encounter_id, session_id=current.id,
                         kind='test.event', payload={}, created_at=1000))
        db.commit()
        first = event_batch(db, current.hospital_id, current.id, 999, '')
        second = event_batch(db, current.hospital_id, current.id, first[-1].created_at, first[-1].id)
        third = event_batch(db, current.hospital_id, current.id, second[-1].created_at, second[-1].id)
        tied = [row.id for row in first + second + third if row.kind == 'test.event']
        assert len(tied) == len(set(tied)) == 205


def test_restore_isolation_cannot_be_disabled_by_an_unverified_switch(client, admin):
    assert client.patch('/api/v1/admin/settings', headers=admin,
                        json={'recovery_isolation': True}).status_code == 200
    response = client.patch('/api/v1/admin/settings', headers=admin,
                            json={'recovery_isolation': False})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'recovery_verification_required'
