import json

import httpx
import pytest
from sqlalchemy import select

from app import clinical, extraction, jobs
from app.config import settings
from app.db import SessionLocal
from app.models import CaptureSession, Fact, FactRevision, Job, NoteRevision, Transcript, TranscriptRevision, User


def source(session, text, speaker="unknown", subject="unknown"):
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "doctor"))
        transcript = clinical.add_transcript(db, user, db.get(CaptureSession, session["id"]), text,
                                             speaker, subject, origin="asr")
        db.commit()
        return clinical.row_json(transcript)


def candidate(transcript, **changes):
    text = transcript["text"]
    result = {
        "concept": "symptom", "section": "history_present", "speaker": "patient", "subject": "patient",
        "polarity": "positive", "certainty": "certain", "elicitation": "asked", "statement_type": "assertion",
        "medication_status": "unknown", "medication": None, "clinical_time": None,
        "evidence": {"transcript_id": transcript["id"], "transcript_revision": transcript["revision"],
                     "char_start": 0, "char_end": len(text), "quote": text},
    }
    return result | changes


@pytest.fixture()
def model(client, monkeypatch):
    monkeypatch.setattr(settings, "model_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_base_url", "https://approved.test/v1")
    monkeypatch.setattr(settings, "llm_model", "clinical-model-v1")
    monkeypatch.setattr(settings, "approved_model_hosts", "approved.test")
    state = {"facts": [], "requests": [], "during_call": None}
    real_client = httpx.Client

    def handle(request):
        assert request.url == "https://approved.test/v1/chat/completions"
        body = json.loads(request.content)
        state["requests"].append(body)
        assert body["response_format"]["json_schema"]["strict"] is True
        if state["during_call"]:
            state["during_call"]()
        return httpx.Response(200, json={"model": "clinical-model-v1", "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"facts": state["facts"]}, ensure_ascii=False),
        }}]})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    return state


def run(client, doctor, session):
    response = client.post(f"/api/v1/sessions/{session['id']}/extract-facts", headers=doctor)
    assert response.status_code == 202, response.text
    claimed = jobs.claim_job(["fact_extraction"])
    assert claimed is not None
    jobs.execute_job(*claimed)
    return client.get("/api/v1/jobs/" + response.json()["id"], headers=doctor).json()


def extracted(client, doctor, session):
    return [fact for fact in client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()
            if fact["origin"] == "llm_extraction"]


def test_approved_http_extraction_is_grounded_unconfirmed_and_versioned(client, doctor, session, model):
    transcript = source(session, "患者说：三天来咳嗽，没有发热。")
    model["facts"] = [candidate(transcript, polarity="negative")]
    result = run(client, doctor, session)
    assert result["state"] == "SUCCEEDED", result
    assert result["result"]["requires_physician_review"] is True
    fact, = extracted(client, doctor, session)
    assert fact["text"] == transcript["text"]
    assert fact["confirmation_status"] == "unconfirmed" and fact["doctor_source"] is False
    assert fact["polarity"] == "negative" and fact["normalized_value"] is None
    evidence = fact["evidence"][0]
    assert evidence["source_revision"] == 1 and evidence["char_end"] == len(transcript["text"])
    with SessionLocal() as db:
        assert db.scalar(select(FactRevision).where(FactRevision.fact_id == fact["id"])).body["evidence"] == fact["evidence"]
        assert db.scalar(select(TranscriptRevision).where(TranscriptRevision.transcript_id == transcript["id"])).body["text"] == transcript["text"]
        raw = db.scalar(select(Fact).where(Fact.session_id == session["id"], Fact.status == "SUPERSEDED"))
        assert raw.body["concept"] == "verbatim_statement"


@pytest.mark.parametrize("corruption,code", [
    ("quote", "ungrounded_extraction"), ("revision", "ungrounded_extraction"),
    ("confirmed", "invalid_extraction"), ("dose", "unsupported_clinical_value"),
])
def test_unsupported_model_output_is_rejected_atomically(client, doctor, session, model, corruption, code):
    transcript = source(session, "我没有发热，药物只吃半片，药名和毫克数记不清。")
    output = candidate(transcript, polarity="negative")
    if corruption == "quote":
        output["evidence"]["quote"] = "我有发热"
    elif corruption == "revision":
        output["evidence"]["transcript_revision"] = 2
    elif corruption == "confirmed":
        output["confirmation_status"] = "confirmed"
    else:
        output.update(concept="medication", medication={"name": None, "formulation": None,
                      "dose": "10 mg", "route": None, "frequency": None})
    model["facts"] = [candidate(transcript), output]
    result = run(client, doctor, session)
    assert result["state"] == "FAILED" and result["error"]["code"] == code
    assert extracted(client, doctor, session) == []


def test_unknown_negation_and_medication_fields_remain_separate(client, doctor, session, model):
    transcript = source(session, "未问过敏史；以前吃过半片药，现在是否服用记不清。")
    model["facts"] = [candidate(transcript, concept="medication", section="medication_history",
                                polarity="unknown", certainty="uncertain", elicitation="unverifiable",
                                medication={"name": None, "formulation": None, "dose": "半片", "route": None, "frequency": None})]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    fact, = extracted(client, doctor, session)
    assert fact["polarity"] == "unknown" and fact["certainty"] == "uncertain"
    assert fact["elicitation"] == "unverifiable" and fact["medication_status"] == "unknown"
    assert fact["medication"]["dose"] == "半片" and fact["normalized_value"] is None


def test_unasked_information_cannot_become_a_negative_finding(client, doctor, session, model):
    transcript = source(session, "未询问青霉素过敏史。")
    model["facts"] = [candidate(transcript, concept="allergy", section="allergies", polarity="negative", elicitation="not_asked")]
    result = run(client, doctor, session)
    assert result["state"] == "FAILED" and result["error"]["code"] == "unsupported_polarity"


def test_family_subject_is_segregated_from_patient_history(client, doctor, session, model):
    transcript = source(session, "我是患者的女儿，我自己有糖尿病。", speaker="family", subject="family")
    model["facts"] = [candidate(transcript, speaker="family", subject="family", concept="history", section="past_history")]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    fact, = extracted(client, doctor, session)
    assert fact["speaker"] == fact["subject"] == "family"
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": "admission"}).json()
    assert next(block for block in note["blocks"] if block["key"] == "past_history")["fact_ids"] == []


def test_model_cannot_override_known_source_identity(client, doctor, session, model):
    transcript = source(session, "我自己有糖尿病。", speaker="family", subject="family")
    model["facts"] = [candidate(transcript)]
    result = run(client, doctor, session)
    assert result["error"]["code"] == "source_identity_mismatch"


@pytest.mark.parametrize("change", ["transcript", "fact", "immutable_source"])
def test_changed_snapshot_is_stale_and_cannot_publish(client, doctor, session, model, change):
    transcript = source(session, "我没有发热。")
    model["facts"] = [candidate(transcript, polarity="negative")]

    def modify():
        if change == "transcript":
            response = client.patch("/api/v1/transcripts/" + transcript["id"], headers=doctor,
                                    json={"base_revision": 1, "text": "患者记不清是否发热。"})
            assert response.status_code == 200
        elif change == "fact":
            fact = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]
            response = client.patch("/api/v1/facts/" + fact["id"], headers=doctor,
                                    json={"base_revision": 1, "text": "患者记不清是否发热。"})
            assert response.status_code == 200
        else:
            with SessionLocal() as db:
                row = db.scalar(select(TranscriptRevision).where(TranscriptRevision.transcript_id == transcript["id"]))
                row.body = row.body | {"text": "Changed immutable source must be detected"}
                db.commit()

    model["during_call"] = modify
    result = run(client, doctor, session)
    assert result["state"] == "STALE", result
    assert extracted(client, doctor, session) == []


@pytest.mark.parametrize("decision", ["corrected", "excluded"])
def test_repeated_extraction_preserves_doctor_decisions_and_locked_blocks(client, doctor, session, model, decision):
    transcript = source(session, "我有咳嗽。")
    model["facts"] = [candidate(transcript)]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    fact, = extracted(client, doctor, session)
    changes = {"text": "医生复核后另行记载：是否咳嗽不详。"} if decision == "corrected" else {"confirmation_status": "excluded", "resolution_reason": "医生已复核并排除此条"}
    response = client.patch("/api/v1/facts/" + fact["id"], headers=doctor, json={"base_revision": 1, **changes})
    assert response.status_code == 200
    protected = response.json()
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": "admission"}).json()
    blocks = [{key: block[key] for key in ("key", "title", "text", "fact_ids")} for block in note["blocks"]]
    blocks[0].update(text="医生锁定的一般情况。", fact_ids=[])
    response = client.patch("/api/v1/notes/" + note["id"], headers=doctor, json={"base_revision": 1, "blocks": blocks})
    assert response.status_code == 200
    note = response.json()
    result = run(client, doctor, session)
    assert result["state"] == "SUCCEEDED" and result["result"]["fact_ids"] == []
    assert result["result"]["skipped_source_ids"] == [transcript["id"]]
    preserved = next(item for item in client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()
                     if item["id"] == fact["id"])
    assert preserved == protected
    with SessionLocal() as db:
        current = db.scalar(select(NoteRevision).where(NoteRevision.note_id == note["id"], NoteRevision.revision == note["revision"]))
        assert current.blocks[0]["text"] == "医生锁定的一般情况。" and current.blocks[0]["protected"] is True
        assert db.scalar(select(FactRevision).where(FactRevision.fact_id == fact["id"], FactRevision.revision == 1)).body["text"] == transcript["text"]


def test_reclaimed_job_cannot_publish_older_generation(client, doctor, session, model):
    transcript = source(session, "我有咳嗽。")
    model["facts"] = [candidate(transcript)]

    def reclaim():
        with SessionLocal() as db:
            job = db.scalar(select(Job).where(Job.kind == "fact_extraction"))
            job.generation += 1
            db.commit()

    model["during_call"] = reclaim
    result = run(client, doctor, session)
    assert result["result"] is None and result["state"] == "RUNNING"
    assert extracted(client, doctor, session) == []


def test_questions_remain_questions_and_cannot_fill_a_note(client, doctor, session, model):
    transcript = source(session, "医生问患者：您有发热吗？")
    model["facts"] = [candidate(transcript, speaker="doctor", polarity="unknown", certainty="unknown", statement_type="question")]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    fact, = extracted(client, doctor, session)
    assert fact["statement_type"] == "question" and fact["polarity"] == "unknown"
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": "admission"}).json()
    assert all(fact["id"] not in block["fact_ids"] for block in note["blocks"])


def test_endpoint_reuses_same_snapshot_job_and_checks_access(client, doctor, session, model):
    source(session, "我有咳嗽。")
    endpoint = f"/api/v1/sessions/{session['id']}/extract-facts"
    first = client.post(endpoint, headers=doctor)
    second = client.post(endpoint, headers=doctor)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert client.post(endpoint).status_code == 401


def test_large_automatic_snapshot_retains_asr_and_reports_unavailable(client, doctor, session, model):
    transcript = source(session, "a" * (extraction.MAX_INPUT_CHARS + 1))
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "doctor"))
        job = extraction.enqueue_extraction(db, user, db.get(CaptureSession, session["id"]), [transcript["id"]], automatic=True)
        assert job is None
        db.commit()
        assert db.get(Transcript, transcript["id"]).status == "AVAILABLE"
    response = client.post(f"/api/v1/sessions/{session['id']}/extract-facts", headers=doctor)
    assert response.status_code == 413


def test_partial_extraction_retains_unextracted_source_content(client, doctor, session, model):
    transcript = source(session, "我有咳嗽。以前青霉素过敏。")
    output = candidate(transcript)
    output["evidence"].update(char_end=len("我有咳嗽。"), quote="我有咳嗽。")
    model["facts"] = [output]
    result = run(client, doctor, session)
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["partially_extracted_source_ids"] == [transcript["id"]]
    facts = client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()
    raw = next(fact for fact in facts if fact["origin"] == "asr")
    assert raw["text"] == transcript["text"] and raw["status"] == "AVAILABLE"


def test_demo_does_not_fabricate_language_understanding(client, doctor, session):
    source(session, "患者有咳嗽。")
    result = run(client, doctor, session)
    assert result["state"] == "FAILED" and result["error"]["code"] == "extraction_unavailable"
    assert extracted(client, doctor, session) == []


def test_http_asr_automatically_queues_extraction(client, doctor, session, model, monkeypatch):
    monkeypatch.setattr(jobs, "read_audio", lambda *args: b"\x00\x00" * 160)
    monkeypatch.setattr(jobs, "transcribe", lambda wav: {"text": "我有咳嗽。", "model_version": "asr-test"})
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "doctor"))
        capture = db.get(CaptureSession, session["id"])
        job = jobs.enqueue(db, user, capture, "asr", {"source_version": 0, "manifest": [
            {"channel_id": "mono", "capture_epoch": "1", "last_seq": 0, "sample_end": 160}]})
        from app.models import Encounter
        job.input = job.input | {"source_version": db.get(Encounter, session["encounter_id"]).source_version}
        job_id = job.id
        db.commit()
    jobs.execute_job(*jobs.claim_job(["asr"]))
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.state == "SUCCEEDED", job.error
        next_job = db.get(Job, job.result["extraction_job_id"])
        assert next_job.kind == "fact_extraction" and next_job.state == "QUEUED"
        assert next_job.input["transcripts"][0]["body"]["text"] == "我有咳嗽。"


@pytest.mark.parametrize("text,quote,changes", [
    ("患者否认发热。", "发热", {}),
    ("去年服用过，现已停用阿司匹林。", "阿司匹林", {"concept": "medication", "medication_status": "current"}),
    ("Patient denies fever.", "fever.", {}),
    ("Previously took aspirin; now stopped.", "now stopped.", {"concept": "medication", "medication_status": "stopped"}),
    ("Dose is 0.5 mg, previously used.", "5 mg, previously used.", {"concept": "medication"}),
])
def test_clipped_negation_medication_and_decimal_context_is_rejected(client, doctor, session, model, text, quote, changes):
    transcript = source(session, text)
    output = candidate(transcript, **changes)
    start = text.index(quote)
    output["evidence"].update(char_start=start, char_end=start + len(quote), quote=quote)
    model["facts"] = [output]
    result = run(client, doctor, session)
    assert result["state"] == "FAILED" and result["error"]["code"] == "incomplete_evidence_context"
    assert extracted(client, doctor, session) == []
    assert client.get(f"/api/v1/sessions/{session['id']}/facts", headers=doctor).json()[0]["text"] == text


@pytest.mark.parametrize("text,status", [
    ("去年服用过阿司匹林。", "historical"),
    ("去年服用过，现已停用阿司匹林。", "stopped"),
    ("目前正在服用阿司匹林。", "current"),
])
def test_medication_status_and_complete_qualifiers_are_retained(client, doctor, session, model, text, status):
    transcript = source(session, text)
    model["facts"] = [candidate(transcript, concept="medication", section="medication_history", medication_status=status,
                                medication={"name": "阿司匹林", "formulation": None, "dose": None, "route": None, "frequency": None})]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    fact, = extracted(client, doctor, session)
    assert fact["text"] == text and fact["medication_status"] == status
    assert fact["confirmation_status"] == "unconfirmed"


@pytest.mark.parametrize("subject,section,text", [
    ("patient", "past_history", "我是患者女儿，我父亲既往有高血压。"),
    ("family", "family_history", "我是患者女儿，我自己有糖尿病。"),
])
def test_family_reporter_and_clinical_subject_remain_independent(client, doctor, session, model, subject, section, text):
    transcript = source(session, text, speaker="family", subject=subject)
    model["facts"] = [candidate(transcript, speaker="family", subject=subject, concept="history", section=section)]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    fact, = extracted(client, doctor, session)
    note = client.post("/api/v1/notes", headers=doctor, json={"session_id": session["id"], "note_type": "admission"}).json()
    assert fact["id"] in next(block for block in note["blocks"] if block["key"] == section)["fact_ids"]
    assert fact["speaker"] == "family" and fact["subject"] == subject


def test_relative_time_keeps_original_evidence_after_later_read(client, doctor, session, model, monkeypatch):
    transcript = source(session, "昨天出现咳嗽。")
    model["facts"] = [candidate(transcript, clinical_time="昨天")]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    original, = extracted(client, doctor, session)
    monkeypatch.setattr(clinical, "now", lambda: original["created_at"] + 86400 * 30)
    later, = extracted(client, doctor, session)
    assert later["clinical_time"] == "昨天" and later["evidence"] == original["evidence"]
    assert later["text"] == transcript["text"]


def test_unstated_absolute_date_cannot_replace_relative_time(client, doctor, session, model):
    transcript = source(session, "昨天出现咳嗽。")
    model["facts"] = [candidate(transcript, clinical_time="2026-09-08")]
    result = run(client, doctor, session)
    assert result["error"]["code"] == "unsupported_clinical_value"


def test_complete_second_sentence_remains_valid(client, doctor, session, model):
    transcript = source(session, "否认发热。 昨天出现咳嗽。")
    quote = "昨天出现咳嗽。"
    output = candidate(transcript, clinical_time="昨天")
    output["evidence"].update(char_start=transcript["text"].index(quote), char_end=len(transcript["text"]), quote=quote)
    model["facts"] = [output]
    assert run(client, doctor, session)["state"] == "SUCCEEDED"
    assert extracted(client, doctor, session)[0]["text"] == quote
