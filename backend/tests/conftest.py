import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from fastapi.testclient import TestClient

TEST_DIR = Path(tempfile.mkdtemp(prefix="his-backend-test-"))
os.environ["HIS_ENV"] = "test"
POSTGRES_TEST_URL = os.environ.get("HIS_TEST_POSTGRES_URL")
TEST_SCHEMA = "his_test_" + uuid.uuid4().hex
if POSTGRES_TEST_URL:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    admin_engine = create_engine(POSTGRES_TEST_URL)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
    parsed_url = make_url(POSTGRES_TEST_URL)
    test_url = parsed_url.update_query_dict({"options": "-csearch_path=" + TEST_SCHEMA})
    os.environ["HIS_DATABASE_URL"] = test_url.render_as_string(hide_password=False)
else:
    os.environ["HIS_DATABASE_URL"] = "sqlite:///" + (TEST_DIR / "test.db").as_posix()
os.environ["HIS_DATA_DIR"] = str(TEST_DIR)
os.environ["HIS_RUN_WORKER"] = "false"

from app.db import SessionLocal, engine
from app.main import app
from app.models import Base
from app.seed import seed


@pytest.fixture(scope="session", autouse=True)
def clean_test_schema():
    yield
    if POSTGRES_TEST_URL:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{TEST_SCHEMA}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def doctor(client):
    result = client.post("/api/v1/auth/login", json={"username": "doctor", "password": "Doctor123!"})
    assert result.status_code == 200
    return {"Authorization": "Bearer " + result.json()["access_token"]}


@pytest.fixture()
def admin(client):
    result = client.post("/api/v1/auth/login", json={"username": "admin", "password": "Admin123!"})
    return {"Authorization": "Bearer " + result.json()["access_token"]}


@pytest.fixture()
def session(client, doctor):
    response = client.post("/api/v1/sessions", headers=doctor, json={"encounter_id": "enc-demo-001", "mode": "dictation"})
    assert response.status_code == 201
    return response.json()


def prepared_note(client, doctor, session, note_type="admission"):
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": note_type}).json()
    blocks = [{"key": b["key"], "title": b["title"], "text": "医生本次明确提供并核实的" + b["title"] + "，用于虚构集成测试。", "fact_ids": []} for b in note["blocks"]]
    patched = client.patch("/api/v1/notes/" + note["id"], headers=doctor, json={"base_revision": note["revision"], "blocks": blocks})
    assert patched.status_code == 200, patched.text
    note = patched.json()
    review = client.post("/api/v1/notes/" + note["id"] + "/review", headers=doctor, json={"base_revision": note["revision"], "issue_resolutions": []})
    assert review.status_code == 200, review.text
    return note, review.json()
