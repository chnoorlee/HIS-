from datetime import datetime, timezone

from sqlalchemy import select

from .config import settings
from .models import AppSetting, Encounter, Source, User
from .security import hash_password


def seed(db):
    if settings.env == "production" or db.scalar(select(User.id).limit(1)):
        return
    stamp = datetime.now(timezone.utc).isoformat()
    records = [
        ("enc-demo-001", "DEMO-P001", "DEMO-A001", "李明（模拟）", "08", 62, "男", "咳嗽、发热待查"),
        ("enc-demo-002", "DEMO-P002", "DEMO-A002", "陈芳（模拟）", "12", 48, "女", "腹痛待查"),
        ("enc-demo-003", "DEMO-P003", "DEMO-A003", "王建国（模拟）", "15", 71, "男", "胸闷待查"),
    ]
    for eid, pid, aid, name, bed, age, sex, diagnosis in records:
        db.add(Encounter(id=eid, hospital_id="hospital-demo", patient_id=pid, admission_id=aid, patient_name=name, bed=bed, department="呼吸与危重症医学科", age=age, sex=sex, diagnosis=diagnosis, admitted_at=stamp, synthetic=True))
    db.flush()
    for username, password, display, roles, grants in [
        ("doctor", "Doctor123!", "张医生（模拟）", ["doctor"], [r[0] for r in records]),
        ("reviewer", "Reviewer123!", "审核医生（模拟）", ["doctor", "reviewer"], [r[0] for r in records]),
        ("admin", "Admin123!", "系统管理员（模拟）", ["admin"], [r[0] for r in records]),
        ("restricted", "Restricted123!", "限权医生（测试）", ["doctor"], [records[1][0]]),
    ]:
        db.add(User(username=username, display_name=display, password_hash=hash_password(password), hospital_id="hospital-demo", roles=roles, encounter_ids=grants))
    for record in records:
        for key, title, body in [
            ("identity", "住院身份（模拟 HIS）", {"patient_id": record[1], "admission_id": record[2], "bed": record[4]}),
            ("vitals", "生命体征（模拟护理记录）", {"temperature": "38.2 ℃", "heart_rate": "92 次/分", "blood_pressure": "128/78 mmHg", "source_note": "虚构测试记录，非真实患者"}),
            ("laboratory", "检验报告（模拟 LIS）", {"white_blood_cells": "10.8 ×10^9/L", "report_status": "初步报告", "source_note": "仅用于测试资料快照"}),
        ]:
            db.add(Source(hospital_id="hospital-demo", encounter_id=record[0], source_system="MOCK_HIS", source_key=key, version=1, title=title, body=body, occurred_at=stamp, recorded_at=stamp))
    db.add(AppSetting(key="hospital-demo", value={"export_enabled": True, "mock_emr_scenario": "normal", "audio_retention_hours": 24, "content_retention_days": 30, "recovery_isolation": False}))
    db.commit()
