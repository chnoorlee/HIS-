from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event, select

from app import clinical, main, schemas
from app.db import SessionLocal, engine
from app.models import AppSetting, CaptureSession, User


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="Concurrent row-lock regressions require HIS_TEST_POSTGRES_URL",
)


def interleave_after_load(model, first_action, second_action):
    loaded, release, committed = Event(), Event(), Event()

    def first():
        with SessionLocal() as db:
            def pause_after_load(_session, instance):
                if isinstance(instance, model) and not loaded.is_set():
                    loaded.set()
                    assert release.wait(10), "Concurrent write was never scheduled"

            event.listen(db, "loaded_as_persistent", pause_after_load)
            first_action(db)

    def second():
        with SessionLocal() as db:
            second_action(db)
            db.commit()
            committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first)
        try:
            assert loaded.wait(10), "First operation did not read its protected row"
            second_result = pool.submit(second)
            # Let the competing transaction commit if the first read lacks a lock.
            committed_early = committed.wait(0.5)
        finally:
            release.set()
        first_result.result(timeout=10)
        second_result.result(timeout=10)
    assert not committed_early, "Competing update committed before the first writer released its row"


@pytest.mark.parametrize("action", ["pause", "resume", "audio_gaps"])
def test_session_transition_cannot_overwrite_concurrent_quarantine(
    client, session, action
):
    session_id = session["id"]

    def transition(db):
        user = db.scalar(select(User).where(User.username == "doctor"))
        if action == "audio_gaps":
            body = schemas.AudioGapsIn(gaps=[{
                "channel_id": "doctor_mic", "capture_epoch": "race-test", "seq": 0,
                "sample_start": 0, "sample_count": 1600, "reason": "Synthetic missing audio",
            }])
            main.audio_gaps(session_id, body, db, user)
        else:
            getattr(main, action)(session_id, db, user)

    def quarantine(db):
        user = db.scalar(select(User).where(User.username == "doctor"))
        capture = db.get(CaptureSession, session_id)
        clinical.quarantine(db, user, capture, "Synthetic concurrent quarantine")

    interleave_after_load(CaptureSession, transition, quarantine)
    with SessionLocal() as db:
        assert db.get(CaptureSession, session_id).status == "QUARANTINED"


def test_retention_update_cannot_disable_concurrent_recovery_isolation(client):
    def retention_change(db):
        admin = db.scalar(select(User).where(User.username == "admin"))
        main.edit_settings({"content_retention_days": 29}, db, admin)

    def isolate(db):
        admin = db.scalar(select(User).where(User.username == "admin"))
        main.edit_settings({"recovery_isolation": True}, db, admin)

    interleave_after_load(AppSetting, retention_change, isolate)
    with SessionLocal() as db:
        settings = db.get(AppSetting, "hospital-demo").value
        assert settings["recovery_isolation"] is True
        assert settings["content_retention_days"] == 29
