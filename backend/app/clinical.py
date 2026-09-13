import hashlib
import json

from sqlalchemy import select, update

from .audio import event
from .models import AudioChunk, CaptureSession, Encounter, Export, Fact, FactRevision, Incident, Job, Note, NoteRevision, Review, Source, Transcript, TranscriptRevision, now
from .security import access_session, audit, fail

TEMPLATES = {
    "admission": [("general_information", "一般情况"), ("chief_complaint", "主诉"), ("history_present", "现病史"), ("past_history", "既往史"), ("medication_history", "用药史"), ("allergies", "过敏史"), ("personal_history", "个人史"), ("marital_reproductive_history", "婚育史"), ("family_history", "家族史"), ("review_of_systems", "系统回顾"), ("physical_exam", "体格检查"), ("investigations", "辅助检查"), ("diagnosis", "初步诊断"), ("plan", "诊疗计划")],
    "first_progress": [("case_features", "病例特点"), ("diagnosis", "初步诊断"), ("diagnostic_analysis", "诊断依据与鉴别分析"), ("plan", "诊疗计划")],
    "daily_progress": [("interval_history", "病情变化"), ("physical_exam", "查体与观察"), ("investigations", "检查结果变化"), ("assessment", "医生评估"), ("plan", "诊疗计划")],
    "discharge": [("admission_diagnosis", "入院诊断"), ("discharge_diagnosis", "出院诊断"), ("hospital_course", "住院经过"), ("discharge_condition", "出院情况"), ("discharge_medications", "出院用药"), ("follow_up", "随访安排")],
}
DOCTOR_ONLY = {"physical_exam", "diagnosis", "diagnostic_analysis", "plan", "assessment", "discharge_diagnosis", "discharge_medications", "follow_up", "discharge_condition"}


def subject_allowed(body, section):
    return body.get("subject") == "patient" or (section == "family_history" and body.get("subject") == "family")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def row_json(row):
    result = {col.name: getattr(row, col.name) for col in row.__table__.columns if col.name not in {"password_hash", "object_path", "token_hash"}}
    if "body" in result:
        result.update(result.pop("body"))
    return result


def invalidate(db, session_id, fact_ids=None, reason="Source changed"):
    db.scalar(select(CaptureSession).where(CaptureSession.id == session_id).with_for_update())
    for note in db.scalars(select(Note).where(Note.session_id == session_id, Note.status != "QUARANTINED")).all():
        revision = db.scalar(select(NoteRevision).where(NoteRevision.note_id == note.id, NoteRevision.revision == note.revision))
        if fact_ids and not set(fact_ids).intersection(revision.facts_snapshot):
            continue
        note.status = "REVIEW_REQUIRED"
        note.review_id = None
        db.execute(update(Review).where(Review.note_id == note.id).values(valid=False))
        for export in db.scalars(select(Export).where(Export.note_id == note.id, Export.status.in_(["CONFIRMED", "SENDING", "UNKNOWN"]))):
            db.add(Incident(hospital_id=note.hospital_id, encounter_id=note.encounter_id, session_id=session_id, reason=reason, dependencies={"note_id": note.id, "export_id": export.id, "target_id": export.target_id}, external_correction_required=True))


def add_transcript(db, user, session, text, speaker, subject, section="history_present", origin="physician_manual", audio_range=None, asr_run_id=None):
    session = access_session(db, user, session.id, for_update=True)
    body = {"text": text, "speaker": speaker, "subject": subject, "section": section, "origin": origin, "audio_range": audio_range, "asr_run_id": asr_run_id, "segment_id": None, "stability": "stable", "actor_id": user.id}
    transcript = Transcript(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, revision=1, body=body)
    db.add(transcript)
    db.flush()
    body = body | {"segment_id": transcript.id}
    transcript.body = body
    db.add(TranscriptRevision(transcript_id=transcript.id, revision=1, body=body, actor_id=user.id))
    fact_body = {
        "text": text, "concept": "verbatim_statement", "raw_value": text, "normalized_value": None, "unit": None,
        "speaker": speaker, "subject": subject, "section": section,
        "polarity": "unknown", "certainty": "unknown", "elicitation": "asked", "conflict_status": "none",
        "confirmation_status": "confirmed" if speaker == "doctor" and origin in {"physician_manual", "synthetic_script"} else "unconfirmed",
        "medication_status": "unknown", "clinical_time": "会话时间；原文中的相对时间未擅自换算", "source_ids": [transcript.id],
        "evidence": [{"source_type": "transcript", "source_id": transcript.id, "source_revision": 1, "char_start": 0, "char_end": len(text), "quote": text, "relationship": "supports", "audio_range": audio_range}],
        "origin": origin, "doctor_source": speaker == "doctor" and origin in {"physician_manual", "synthetic_script"}, "resolution_reason": None,
    }
    fact = Fact(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, revision=1, body=fact_body)
    db.add(fact)
    db.flush()
    db.add(FactRevision(fact_id=fact.id, revision=1, body=fact_body, actor_id=user.id))
    invalidate(db, session.id, reason="New clinical source was added after the previous document snapshot")
    event(db, session, "transcript.created", {"transcript_id": transcript.id, "fact_id": fact.id})
    audit(db, user, "transcript.create", transcript.id, origin=origin)
    return transcript


def patch_transcript(db, user, transcript, body):
    access_session(db, user, transcript.session_id, for_update=True)
    db.refresh(transcript)
    if transcript.revision != body.base_revision:
        fail("revision_conflict", "Transcript was modified; reload current revision", 409, current_revision=transcript.revision)
    changes = body.model_dump(exclude_unset=True, exclude={"base_revision"})
    if any(v is None for v in changes.values()):
        fail("invalid_correction", "Transcript corrections cannot be null")
    new_body = transcript.body | changes | {"corrected_by": user.id}
    revision = transcript.revision + 1
    updated = db.execute(update(Transcript).where(Transcript.id == transcript.id, Transcript.revision == body.base_revision).values(revision=revision, body=new_body))
    if updated.rowcount != 1:
        fail("revision_conflict", "Transcript was modified concurrently", 409)
    db.add(TranscriptRevision(transcript_id=transcript.id, revision=revision, body=new_body, actor_id=user.id))
    impacted = []
    for fact in db.scalars(select(Fact).where(Fact.session_id == transcript.session_id)).all():
        if transcript.id not in fact.body.get("source_ids", []):
            continue
        confirmation = "excluded" if fact.body.get("confirmation_status") == "excluded" else "unconfirmed"
        fact.body = fact.body | {"confirmation_status": confirmation, "source_changed": True}
        fact.revision += 1
        db.add(FactRevision(fact_id=fact.id, revision=fact.revision, body=fact.body, actor_id=user.id))
        impacted.append(fact.id)
    invalidate(db, transcript.session_id, impacted, "Transcript role, subject or content corrected")
    audit(db, user, "transcript.correct", transcript.id, revision=revision)
    db.commit()
    db.refresh(transcript)
    return row_json(transcript)


def patch_fact(db, user, fact, body):
    access_session(db, user, fact.session_id, for_update=True)
    db.refresh(fact)
    if fact.revision != body.base_revision:
        fail("revision_conflict", "Fact was modified; reload current revision", 409, current_revision=fact.revision)
    changes = body.model_dump(exclude_unset=True, exclude={"base_revision"})
    if any(value is None for key, value in changes.items() if key not in {"unit", "resolution_reason"}):
        fail("invalid_correction", "Clinical fact fields cannot be null")
    if changes.get("confirmation_status") == "excluded" or changes.get("conflict_status") == "resolved":
        if not changes.get("resolution_reason"):
            fail("resolution_required", "Excluding or resolving a fact requires a physician reason")
    if fact.body.get("source_changed") and changes.get("confirmation_status") == "confirmed" and not changes.get("resolution_reason"):
        fail("source_recheck_required", "Confirm corrected source content and record your verification")
    new_body = fact.body | changes
    clinical_changes = set(changes) - {"confirmation_status", "resolution_reason", "conflict_status"}
    if clinical_changes or (changes.get("confirmation_status") == "confirmed" and changes.get("resolution_reason")):
        new_body.update({"doctor_source": True, "corrected_by": user.id, "corrected_at": now(), "origin": "physician_correction"})
        new_body["evidence"] = list(new_body.get("evidence", [])) + [{"source_type": "physician", "source_id": user.id, "quote": new_body["text"], "recorded_at": now(), "relationship": "supports"}]
    if changes.get("confirmation_status") == "confirmed":
        new_body["source_changed"] = False
    revision = fact.revision + 1
    updated = db.execute(update(Fact).where(Fact.id == fact.id, Fact.revision == body.base_revision).values(revision=revision, body=new_body))
    if updated.rowcount != 1:
        fail("revision_conflict", "Fact was modified concurrently", 409)
    db.add(FactRevision(fact_id=fact.id, revision=revision, body=new_body, actor_id=user.id))
    invalidate(db, fact.session_id, [fact.id], "Clinical fact corrected")
    audit(db, user, "fact.correct", fact.id, revision=revision, changed_fields=list(changes))
    db.commit()
    db.refresh(fact)
    return row_json(fact)


def session_facts(db, session_id):
    return db.scalars(select(Fact).where(Fact.session_id == session_id, Fact.status == "AVAILABLE").order_by(Fact.created_at)).all()


def build_blocks(note_type, facts):
    blocks = []
    for key, title in TEMPLATES[note_type]:
        matching = [f for f in facts if f.body.get("section") == key and subject_allowed(f.body, key) and f.body.get("statement_type") != "question" and f.body.get("confirmation_status") != "excluded" and (key not in DOCTOR_ONLY or f.body.get("doctor_source"))]
        blocks.append({"key": key, "title": title, "text": "\n".join(f.body["text"] for f in matching), "fact_ids": [f.id for f in matching], "author": "model", "protected": False, "provider": "extractive-template-1.0"})
    return blocks


def note_revision(db, note):
    return db.scalar(select(NoteRevision).where(NoteRevision.note_id == note.id, NoteRevision.revision == note.revision))


def persist_revision(db, user, note, blocks, source_version):
    ids = {fid for block in blocks for fid in block["fact_ids"]}
    facts = {f.id: f for f in session_facts(db, note.session_id)}
    if not ids.issubset(facts):
        fail("foreign_fact", "Note references a fact outside this session or an unavailable fact", 409)
    if any(facts[fid].body.get("confirmation_status") == "excluded" for fid in ids):
        fail("excluded_fact", "Excluded facts cannot be restored implicitly", 409)
    snapshot = {}
    for block in blocks:
        versions = block.get("facts_snapshot", {})
        block["facts_snapshot"] = {fid: versions.get(fid, facts[fid].revision) for fid in block["fact_ids"]}
        for fid, version in block["facts_snapshot"].items():
            # A fact used in several sections stays stale until every section is reconciled.
            snapshot[fid] = min(snapshot.get(fid, version), version)
    snapshot = dict(sorted(snapshot.items()))
    content_digest = digest({"blocks": blocks, "facts": snapshot, "source_version": source_version})
    db.add(NoteRevision(note_id=note.id, revision=note.revision, blocks=blocks, facts_snapshot=snapshot, source_version=source_version, digest=content_digest, actor_id=user.id))


def create_note(db, user, session, note_type):
    session = access_session(db, user, session.id, for_update=True)
    note = Note(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, note_type=note_type, revision=1)
    db.add(note)
    db.flush()
    persist_revision(db, user, note, build_blocks(note_type, session_facts(db, session.id)), db.get(Encounter, session.encounter_id).source_version)
    event(db, session, "note.created", {"note_id": note.id, "revision": 1})
    audit(db, user, "note.create", note.id, note_type=note_type)
    db.commit()
    return note_json(db, note)


def patch_note(db, user, note, body):
    access_session(db, user, note.session_id, for_update=True)
    db.refresh(note)
    if note.revision != body.base_revision:
        fail("revision_conflict", "Note was modified; reload before applying your edits", 409, current_revision=note.revision)
    valid_keys = {k for k, _ in TEMPLATES[note.note_type]}
    if len({b.key for b in body.blocks}) != len(body.blocks) or {b.key for b in body.blocks} != valid_keys:
        fail("invalid_sections", "Note must retain each template section exactly once")
    previous_revision = note_revision(db, note)
    previous = {b["key"]: b for b in previous_revision.blocks}
    facts = {f.id: f for f in session_facts(db, note.session_id)}
    blocks = []
    for block in body.blocks:
        old = previous[block.key]
        changed = old["text"] != block.text or old["fact_ids"] != block.fact_ids
        if not set(block.fact_ids).issubset(facts):
            fail("foreign_fact", "Note references a fact outside this session or an unavailable fact", 409)
        old_versions = old.get("facts_snapshot", previous_revision.facts_snapshot)
        versions = {fid: old_versions.get(fid, facts[fid].revision) if fid in old["fact_ids"] else facts[fid].revision for fid in block.fact_ids}
        if block.reference_review:
            reviewed = block.reference_review.fact_revisions
            if set(reviewed) != set(block.fact_ids):
                fail("invalid_reference_review", "Reference review must identify every fact in this section", 409)
            if any(facts[fid].revision != version for fid, version in reviewed.items()):
                fail("fact_revision_conflict", "A referenced fact changed during review; reload the source before confirming", 409)
            versions = dict(reviewed)
            changed = True
        value = {"key": block.key, "title": dict(TEMPLATES[note.note_type])[block.key], "text": block.text, "fact_ids": block.fact_ids, "author": "doctor" if changed else old["author"], "protected": True if changed else old["protected"]}
        if changed:
            value.update({"actor_id": user.id, "edited_at": now(), "physician_source": {"actor_id": user.id, "recorded_at": now()}})
        else:
            value.update({k: v for k, v in old.items() if k not in value})
        value["facts_snapshot"] = versions
        if block.reference_review:
            value["reference_review"] = {"fact_revisions": versions, "reason": block.reference_review.reason,
                                         "actor_id": user.id, "reviewed_at": now()}
        blocks.append(value)
    result = db.execute(update(Note).where(Note.id == note.id, Note.revision == body.base_revision).values(revision=body.base_revision + 1, status="REVIEW_REQUIRED", review_id=None))
    if result.rowcount != 1:
        fail("revision_conflict", "Note was modified concurrently", 409)
    db.refresh(note)
    persist_revision(db, user, note, blocks, db.get(Encounter, note.encounter_id).source_version)
    db.execute(update(Review).where(Review.note_id == note.id).values(valid=False))
    audit(db, user, "note.edit", note.id, revision=note.revision)
    event(db, db.get(CaptureSession, note.session_id), "note.changed", {"note_id": note.id, "revision": note.revision})
    db.commit()
    return note_json(db, note)


def review_issues(db, note):
    rev = note_revision(db, note)
    encounter = db.get(Encounter, note.encounter_id)
    session = db.get(CaptureSession, note.session_id)
    issues = []
    def add(code, message, key=None, fid=None, resolvable=False, severity="blocker"):
        target = f"{key}:{fid}" if key and fid else key or fid or note.id
        issues.append({"id": f"{code}:{target}", "code": code, "message": message, "block_key": key, "fact_id": fid, "resolvable": resolvable, "severity": severity})
    for block in rev.blocks:
        if not block["text"].strip():
            add("missing_section", f"{block['title']}缺少明确来源，请医生补充；系统不能自动补正常或诊断。", block["key"])
        if block["text"].strip() and block["author"] != "doctor" and not block["fact_ids"]:
            add("unsupported_assertion", "自动生成内容缺少事实来源", block["key"])
        if block["text"].strip() and block["author"] != "doctor":
            selected = [db.get(Fact, fid) for fid in block["fact_ids"]]
            if any(not f or f.status != "AVAILABLE" for f in selected) or block["text"] != "\n".join(f.body["text"] for f in selected if f):
                add("source_text_changed", "引用事实内容已变更，请核对原文并更新章节。", block["key"])
            if block["key"] in DOCTOR_ONLY and any(not f or not f.body.get("doctor_source") for f in selected):
                add("clinical_source_invalid", "本章节需要医生观察或明确判断作为来源。", block["key"])
    if rev.source_version != encounter.source_version:
        add("source_changed", "院内资料版本已变化，请核对并保存新的文书版本。")
    if session.status == "INCOMPLETE":
        add("audio_incomplete", "录音存在缺片；请说明补充信息和处置方式。", resolvable=True)
    elif session.status in {"RECORDING", "FINALIZING"}:
        add("recording_active", "录音或最终复核尚未结束。")
    pending_asr = db.scalar(select(Job.id).where(Job.session_id == session.id, Job.kind.in_(["asr", "asr_live"]), Job.state.in_(["QUEUED", "RUNNING", "RETRY_WAIT"])).limit(1))
    if pending_asr:
        add("asr_pending", "语音识别或最终复核尚在处理，请等待结果或通过医院允许的人工流程处置。")
    pending_extraction = db.scalar(select(Job.id).where(Job.session_id == session.id, Job.kind == "fact_extraction", Job.state.in_(["QUEUED", "RUNNING", "RETRY_WAIT"])).limit(1))
    if pending_extraction:
        add("extraction_pending", "临床事实提取尚在处理，请等待结果并核实后再审核。")
    sources = db.scalars(select(Source).where(Source.encounter_id == encounter.id, Source.status == "AVAILABLE")).all()
    if sources and min(now() - s.created_at for s in sources) > __import__("app.config", fromlist=["settings"]).settings.source_max_age_seconds:
        add("source_stale", "院内资料快照已超过配置时限，请刷新。")
    facts = {f.id: f for f in session_facts(db, note.session_id)}
    applicable_sections = {key for key, _ in TEMPLATES[note.note_type]}
    for fid, fact in facts.items():
        body = fact.body
        section = body.get("section")
        if (fid not in rev.facts_snapshot and section in applicable_sections
                and (subject_allowed(body, section) or body.get("subject") == "unknown")
                and body.get("confirmation_status") != "excluded" and body.get("statement_type") != "question"):
            add("unincorporated_fact", "本章节有尚未处置的会话事实，请核实并纳入文书，或说明原因后排除。", key=section, fid=fid)
    for fid, version in rev.facts_snapshot.items():
        fact = facts.get(fid)
        if not fact:
            add("source_unavailable", "事实或来源已不可用。", fid=fid)
            continue
        body = fact.body
        if body.get("statement_type") == "question":
            add("question_not_assertion", "询问内容不能作为已发生的临床事实写入文书。", fid=fid)
        for block in rev.blocks:
            if fid in block["fact_ids"] and (fact.revision != block.get("facts_snapshot", rev.facts_snapshot).get(fid, version) or body.get("source_changed")):
                add("fact_changed", "引用事实已经更正，请逐章节核对引用并记录核对说明。", key=block["key"], fid=fid)
        associated_keys = [b["key"] for b in rev.blocks if fid in b["fact_ids"]]
        if any(not subject_allowed(body, key) for key in associated_keys):
            add("subject_mismatch", "该陈述的临床主体不是患者，不能写入患者断言。", fid=fid)
        if body.get("confirmation_status") != "confirmed":
            add("fact_unconfirmed", "事实尚未由医生核实。", fid=fid)
        if body.get("conflict_status") == "unresolved":
            add("clinical_conflict", "关键事实仍有冲突，需要逐项纠正或排除。", fid=fid)
        if body.get("certainty") in {"uncertain", "suspected", "unknown"}:
            add("uncertainty_preserved", "保留原始不确定表述，未自动推为确诊。", fid=fid, severity="warning")
        for evidence in body.get("evidence", []):
            if evidence.get("source_type") == "transcript":
                source = db.get(Transcript, evidence["source_id"])
                if not source or source.status != "AVAILABLE":
                    add("source_unavailable", "转写证据已不可用，需重新核实。", fid=fid)
                elif evidence.get("audio_range"):
                    r = evidence["audio_range"]
                    expired = db.scalar(select(AudioChunk.id).where(AudioChunk.session_id == note.session_id, AudioChunk.channel_id == r["channel_id"], AudioChunk.capture_epoch == str(r["capture_epoch"]), AudioChunk.expires_at < now()).limit(1))
                    if expired and body.get("confirmation_status") != "confirmed":
                        add("audio_expired", "未核实事实的音频已过期，请医生重新核实。", fid=fid)
    return issues


def note_json(db, note):
    rev = note_revision(db, note)
    return row_json(note) | {"blocks": rev.blocks, "digest": rev.digest, "facts_snapshot": rev.facts_snapshot, "source_version": rev.source_version, "template_version": rev.template_version, "issues": review_issues(db, note), "revisions": [{"revision": r.revision, "created_at": r.created_at, "actor_id": r.actor_id, "digest": r.digest, "blocks": r.blocks} for r in db.scalars(select(NoteRevision).where(NoteRevision.note_id == note.id).order_by(NoteRevision.revision.desc())).all()]}


def review_note(db, user, note, body):
    access_session(db, user, note.session_id, for_update=True)
    db.refresh(note)
    if body.base_revision != note.revision:
        fail("revision_conflict", "Only the current exact note revision can be reviewed", 409, current_revision=note.revision)
    issues = review_issues(db, note)
    resolutions = {r.issue_id: r.resolution for r in body.issue_resolutions}
    valid_resolution_ids = {i["id"] for i in issues if i["resolvable"]}
    if set(resolutions) - valid_resolution_ids:
        fail("invalid_resolution", "Clinical blockers require correction, not blanket acknowledgement", 409)
    blockers = [i for i in issues if i["severity"] == "blocker" and not (i["resolvable"] and i["id"] in resolutions)]
    if blockers:
        fail("review_blocked", "请先处理文书缺项与事实核实项目。", 409, issues=blockers)
    rev = note_revision(db, note)
    review = Review(note_id=note.id, note_revision=note.revision, actor_id=user.id, digest=rev.digest, facts_snapshot=rev.facts_snapshot, source_version=rev.source_version, issue_resolutions=[r.model_dump() for r in body.issue_resolutions])
    db.add(review)
    db.flush()
    result = db.execute(update(Note).where(Note.id == note.id, Note.revision == body.base_revision).values(status="REVIEWED", review_id=review.id))
    if result.rowcount != 1:
        fail("revision_conflict", "Note changed during review", 409)
    audit(db, user, "note.review", note.id, revision=note.revision, digest=rev.digest)
    db.commit()
    return row_json(review)


def quarantine(db, user, session, reason):
    session = access_session(db, user, session.id, allow_quarantined=True, for_update=True)
    if session.status == "DELETED":
        fail("deleted_session", "Deleted sessions cannot be reopened for quarantine", 409)
    session.status = "QUARANTINED"
    session.generation += 1
    session.revision += 1
    counts = {}
    for model in [Transcript, Fact, Note, AudioChunk]:
        rows = db.scalars(select(model).where(model.session_id == session.id)).all()
        counts[model.__tablename__] = len(rows)
        for row in rows:
            row.status = "QUARANTINED"
            if isinstance(row, Note):
                row.review_id = None
                db.execute(update(Review).where(Review.note_id == row.id).values(valid=False))
    db.execute(update(Job).where(Job.session_id == session.id, Job.state.in_(["QUEUED", "RUNNING", "RETRY_WAIT"])).values(state="CANCELLED", generation=Job.generation + 1))
    exports = db.scalars(select(Export).join(Note, Note.id == Export.note_id).where(Note.session_id == session.id)).all()
    counts["exports"] = [e.id for e in exports]
    for operation in exports:
        if operation.status == "PREPARED":
            operation.status, operation.active_key = "CANCELLED", None
    external = any(e.status in {"CONFIRMED", "UNKNOWN", "SENDING"} for e in exports)
    incident = Incident(hospital_id=session.hospital_id, encounter_id=session.encounter_id, session_id=session.id, reason=reason, dependencies=counts, external_correction_required=external)
    db.add(incident)
    event(db, session, "session.quarantined", {"status": "QUARANTINED"})
    audit(db, user, "session.quarantine", session.id, dependent_counts=counts)
    return incident


DEMO_SCRIPT = [
    ("general_information", "doctor", "patient", "模拟患者李明，男，62岁；本次就诊信息已与模拟住院身份核对。"),
    ("chief_complaint", "doctor", "patient", "主诉：咳嗽、发热3天。"),
    ("history_present", "patient", "patient", "3天前开始咳嗽，有少量白痰，昨天发热，最高体温记不清。"),
    ("past_history", "family", "family", "家属陈述：我本人有糖尿病。这句话说的是家属本人，不是患者。"),
    ("past_history", "doctor", "patient", "医生询问并确认：患者既往有高血压病史，病程约10年。"),
    ("allergies", "doctor", "patient", "药物过敏史暂不详，患者记不清既往皮疹与具体药物的关系，需进一步核实。"),
    ("medication_history", "doctor", "patient", "患者自述有降压药使用史，具体药名、规格、剂量及当前服用情况尚未核实。"),
    ("personal_history", "doctor", "patient", "个人史尚未完整询问，吸烟、饮酒及职业暴露情况待补充。"),
    ("marital_reproductive_history", "doctor", "patient", "婚育史尚未询问，后续按科室要求补充。"),
    ("family_history", "doctor", "patient", "家族史尚未完成核实；家属本人患糖尿病的陈述不能直接作为患者病史。"),
    ("review_of_systems", "doctor", "patient", "系统回顾尚未完成，未询问项目不记录为否认或正常。"),
    ("physical_exam", "doctor", "patient", "医生本次查体：双肺呼吸音粗，右下肺可闻及少量湿啰音。"),
    ("investigations", "doctor", "patient", "医生核对模拟检验报告：白细胞10.8×10^9/L，报告为初步结果。"),
    ("diagnosis", "doctor", "patient", "医生初步判断：下呼吸道感染可能，尚需结合检查进一步明确。"),
    ("plan", "doctor", "patient", "医生提供计划：完善胸部影像检查，复核过敏史，动态观察体温及呼吸情况。具体用药待医嘱确认。"),
]
