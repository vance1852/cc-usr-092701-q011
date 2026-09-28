"""跨诊所转诊交接：目的绑定、章节白名单快照、接受前不建档与撤回/过期封存。"""

from __future__ import annotations

import hashlib
from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id, require_id, require_idempotency_key
from .security import authorize, principal_for
from .validation import choice, parsed_timestamp, require_match, text, timestamp

# 患者可明确授权的资料章节。联系方式密文、凭据、内部合并字段等永远不在白名单内。
REFERRAL_SECTIONS = {
    "profile", "assessments", "plans", "observations",
    "clinical_flags", "encounters", "incidents", "followups",
}

REFERRAL_PURPOSES = {
    "further_assessment", "specialty_consult", "continuity_of_care", "second_opinion",
}

PERMISSION = "referral:manage"
CLINICAL_ROLES = {"clinician", "owner"}


class ReferralService:
    """转诊交接用例。所有方法以已解析 JSON 值为边界。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ 创建

    def create_referral(self, clinic_id: str, actor_id: str, patient_id: str, destination_clinic_id: str,
                        designated_staff_id: str, purpose: str, sections: list[str], expires_at: str,
                        consent_id: str, *, note: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        require_id(patient_id, "患者编号")
        require_id(destination_clinic_id, "接收诊所编号")
        require_id(designated_staff_id, "接收医生编号")
        require_id(consent_id, "专项授权编号")
        purpose = choice(purpose, "转诊目的", REFERRAL_PURPOSES)
        sections = self._validate_sections(sections)
        note = text(note, "转诊说明", minimum=0, maximum=1000) if note is not None else None
        key = require_idempotency_key(idempotency_key) if idempotency_key else None
        now = timestamp(self.clock.now())
        expires = timestamp(expires_at, "交接有效期限")
        if parsed_timestamp(expires) <= parsed_timestamp(now):
            raise ValidationError("交接有效期限必须晚于当前时间")
        request = {"source_clinic_id": clinic_id, "patient_id": patient_id,
                   "destination_clinic_id": destination_clinic_id, "designated_staff_id": designated_staff_id,
                   "consent_id": consent_id, "purpose": purpose, "note": note,
                   "sections": sections, "expires_at": expires}
        request_hash = hashlib.sha256(encode_json(request).encode("utf-8")).hexdigest()
        referral_id = new_id("ref")
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, PERMISSION, clinic_id=clinic_id)
            if destination_clinic_id == clinic_id:
                raise ValidationError("接收诊所必须是其他诊所")
            if connection.execute("SELECT 1 FROM clinics WHERE id=? AND state='active'", (destination_clinic_id,)).fetchone() is None:
                raise NotFound("接收诊所不存在或已暂停")
            designee = connection.execute(
                "SELECT id,role,active FROM staff WHERE id=? AND clinic_id=?",
                (designated_staff_id, destination_clinic_id)).fetchone()
            if designee is None or not designee["active"] or designee["role"] not in CLINICAL_ROLES:
                raise ValidationError("接收医生必须是接收诊所的在岗临床岗位")
            patient = connection.execute("SELECT * FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能发起转诊")
            consent = self._require_consent(connection, consent_id, patient_id, sections, now)
            if key:
                keyed = connection.execute(
                    "SELECT id FROM referrals WHERE idempotency_key=? AND source_clinic_id=?",
                    (key, clinic_id)).fetchone()
                if keyed:
                    existing = connection.execute("SELECT * FROM referrals WHERE id=?", (keyed["id"],)).fetchone()
                    if existing["request_hash"] != request_hash:
                        raise Conflict("幂等编号已被其他交接使用")
                    return self._metadata(existing, replayed=True)
            duplicate = connection.execute(
                "SELECT * FROM referrals WHERE request_hash=? AND sealed_at IS NULL "
                "AND state IN ('pending','accepted') ORDER BY created_at DESC,id DESC LIMIT 1",
                (request_hash,)).fetchone()
            if duplicate is not None:
                return self._metadata(duplicate, replayed=True)
            data = self._build_data(connection, patient, sections)
            snapshot = {"format": "careflow-referral-snapshot-v1", "captured_at": now,
                        "sections": sections, "data": data}
            source_digest = self._digest_data(data)
            connection.execute(
                "INSERT INTO referrals(id,source_clinic_id,destination_clinic_id,source_patient_id,created_by,"
                "designated_staff_id,consent_id,purpose,note,sections_json,snapshot_json,source_digest,request_hash,"
                "idempotency_key,state,expires_at,created_at,version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,1)",
                (referral_id, clinic_id, destination_clinic_id, patient_id, actor_id, designated_staff_id,
                 consent_id, purpose, note, encode_json(sections), encode_json(snapshot), source_digest,
                 request_hash, key, expires, now))
            payload = {"destination_clinic_id": destination_clinic_id, "designated_staff_id": designated_staff_id,
                       "purpose": purpose, "sections": sections, "expires_at": expires,
                       "consent_id": consent_id, "source_digest": source_digest}
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.created",
                               occurred_at=now, payload=payload)
            # 接收侧同步留痕：此时接收诊所没有、也不允许有该患者档案，故 patient_id 为空。
            audit.append_event(connection, clinic_id=destination_clinic_id, actor_id=None, patient_id=None,
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.inbound_pending",
                               occurred_at=now, payload={"source_clinic_id": clinic_id, "purpose": purpose,
                                                         "sections": sections, "expires_at": expires})
            row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
        return self._metadata(row, replayed=False)

    # ------------------------------------------------------------ 接受/拒绝

    def respond_referral(self, clinic_id: str, actor_id: str, referral_id: str, decision: str,
                         expected_version: int, *, decline_reason: str | None = None) -> dict[str, Any]:
        require_id(referral_id, "交接编号")
        decision = choice(decision, "接收决定", {"accept", "decline"})
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, PERMISSION, clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM referrals WHERE id=? AND destination_clinic_id=?",
                                     (referral_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("转诊交接不存在")
            if row["designated_staff_id"] != actor_id and principal.role != "owner":
                raise Forbidden("只有被指定的接收医生可以答复该交接")
            require_match(row["version"], expected_version, "转诊交接")
            self._seal_if_needed(connection, row, now)
            row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
            if row["state"] != "pending":
                raise Conflict("只有待答复的交接可以接受或拒绝", details={"state": row["state"]})
            if row["sealed_at"] is not None:
                raise Conflict("交接授权已撤回或已过期，不能接受")
            if decision == "decline":
                decline_reason = text(decline_reason or "", "拒绝原因", maximum=1000)
            target = "accepted" if decision == "accept" else "declined"
            connection.execute(
                "UPDATE referrals SET state=?,responded_by=?,responded_at=?,decline_reason=?,version=version+1 WHERE id=?",
                (target, actor_id, now, decline_reason if decision == "decline" else None, referral_id))
            # 关键不变量：接受路径不执行任何 patients 写入，患者不会因接受而变成接收诊所的在诊档案。
            dest_payload = {"source_clinic_id": row["source_clinic_id"], "decision": target,
                            "reason": decline_reason, "version": expected_version + 1}
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="referral", aggregate_id=referral_id,
                               action=f"referral.{target}", occurred_at=now, payload=dest_payload)
            audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=None,
                               patient_id=row["source_patient_id"], aggregate_type="referral",
                               aggregate_id=referral_id, action=f"referral.{target}_at_destination",
                               occurred_at=now, payload={"destination_clinic_id": clinic_id, "decision": target,
                                                         "reason": decline_reason})
            row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
        return self._metadata(row)

    # ---------------------------------------------------------------- 查看

    def get_referral(self, clinic_id: str, actor_id: str, referral_id: str) -> dict[str, Any]:
        require_id(referral_id, "交接编号")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, PERMISSION, clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
            if row is None or (row["source_clinic_id"] != clinic_id and row["destination_clinic_id"] != clinic_id):
                raise NotFound("转诊交接不存在")
            self._seal_if_needed(connection, row, now)
            row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
            is_destination = row["destination_clinic_id"] == clinic_id
            result = self._metadata(row)
            if not is_destination:
                return result
            if row["designated_staff_id"] != actor_id and principal.role != "owner":
                raise Forbidden("只有被指定的接收医生可以查看该交接")
            if row["state"] != "accepted":
                # 接受前与拒绝后都只能看到信封信息，看不到任何临床章节。
                return result
            snapshot = decode_json(row["snapshot_json"])
            retained = [section for section in snapshot["sections"] if section in snapshot["data"]]
            accessible_snapshot = {"format": snapshot["format"], "captured_at": snapshot["captured_at"],
                                   "sections": retained, "data": {k: snapshot["data"][k] for k in retained}}
            if retained:
                connection.execute(
                    "INSERT INTO referral_access(id,referral_id,clinic_id,staff_id,sections_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (new_id("rac"), referral_id, clinic_id, actor_id, encode_json(retained), now))
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="referral", aggregate_id=referral_id,
                                   action="referral.snapshot_accessed", occurred_at=now,
                                   payload={"sections": retained, "sealed": row["sealed_at"] is not None})
                audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=None,
                                   patient_id=row["source_patient_id"], aggregate_type="referral",
                                   aggregate_id=referral_id, action="referral.snapshot_viewed_at_destination",
                                   occurred_at=now,
                                   payload={"destination_clinic_id": clinic_id, "staff_id": actor_id,
                                            "sections": retained})
            # 源记录变化只产生布尔提示，绝不返回新数据，避免静默扩大披露；封存后不再提示。
            if row["sealed_at"] is None:
                patient = connection.execute("SELECT * FROM patients WHERE id=? AND clinic_id=?",
                                             (row["source_patient_id"], row["source_clinic_id"])).fetchone()
                live_digest = None
                if patient is not None:
                    live_data = self._build_data(connection, patient, retained)
                    live_digest = self._digest_data(live_data)
                result["source_changed"] = bool(live_digest is not None and live_digest != self._retained_digest(row, retained))
            else:
                result["source_changed"] = False
            result["snapshot"] = accessible_snapshot
            return result

    def list_referrals(self, clinic_id: str, actor_id: str, direction: str,
                       *, patient_id: str | None = None) -> dict[str, Any]:
        direction = choice(direction, "查询方向", {"incoming", "outgoing"})
        with self.db.transaction(write=False) as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, PERMISSION, clinic_id=clinic_id)
            if direction == "incoming":
                # 最小披露：医生只能看到指定给自己的交接信封，负责人可看全所队列。
                if principal.role == "owner":
                    rows = connection.execute(
                        "SELECT * FROM referrals WHERE destination_clinic_id=? ORDER BY created_at DESC,id DESC",
                        (clinic_id,)).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM referrals WHERE destination_clinic_id=? AND designated_staff_id=? "
                        "ORDER BY created_at DESC,id DESC", (clinic_id, actor_id)).fetchall()
            else:
                sql = "SELECT * FROM referrals WHERE source_clinic_id=?"
                params: list[Any] = [clinic_id]
                if patient_id:
                    sql += " AND source_patient_id=?"
                    params.append(require_id(patient_id, "患者编号"))
                sql += " ORDER BY created_at DESC,id DESC"
                rows = connection.execute(sql, params).fetchall()
            return {"items": [self._metadata(row) for row in rows]}

    # ------------------------------------------------------------ 重新获取

    def refresh_referral(self, clinic_id: str, actor_id: str, referral_id: str, expires_at: str,
                         *, idempotency_key: str | None = None) -> dict[str, Any]:
        """来源侧基于最新记录冻结新快照并产生新编号；旧交接标记为被取代，不静默扩大披露。"""
        require_id(referral_id, "交接编号")
        expires = timestamp(expires_at, "交接有效期限")
        now = timestamp(self.clock.now())
        if parsed_timestamp(expires) <= parsed_timestamp(now):
            raise ValidationError("交接有效期限必须晚于当前时间")
        key = require_idempotency_key(idempotency_key) if idempotency_key else None
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, PERMISSION, clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM referrals WHERE id=? AND source_clinic_id=?",
                                     (referral_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("转诊交接不存在")
            if row["state"] not in {"pending", "accepted"} or row["sealed_at"] is not None:
                raise Conflict("只有仍在授权期内的交接可以重新获取")
            sections = decode_json(row["sections_json"])
            patient = connection.execute("SELECT * FROM patients WHERE id=? AND clinic_id=?",
                                         (row["source_patient_id"], clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能重新获取交接")
            consent = self._require_consent(connection, row["consent_id"], patient["id"], sections, now)
            if key and connection.execute(
                "SELECT 1 FROM referrals WHERE idempotency_key=? AND source_clinic_id=?",
                (key, clinic_id)).fetchone():
                raise Conflict("幂等编号已被使用")
            new_referral_id = new_id("ref")
            data = self._build_data(connection, patient, sections)
            snapshot = {"format": "careflow-referral-snapshot-v1", "captured_at": now,
                        "sections": sections, "data": data}
            request = {"source_clinic_id": clinic_id, "patient": patient["id"],
                       "destination_clinic_id": row["destination_clinic_id"],
                       "designated_staff_id": row["designated_staff_id"], "consent_id": consent["id"],
                       "purpose": row["purpose"], "note": row["note"], "sections": sections,
                       "expires_at": expires, "refreshed_from": referral_id}
            request_hash = hashlib.sha256(encode_json(request).encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO referrals(id,source_clinic_id,destination_clinic_id,source_patient_id,created_by,"
                "designated_staff_id,consent_id,purpose,note,sections_json,snapshot_json,source_digest,request_hash,"
                "idempotency_key,state,expires_at,created_at,version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,1)",
                (new_referral_id, clinic_id, row["destination_clinic_id"], patient["id"], actor_id,
                 row["designated_staff_id"], consent["id"], row["purpose"], row["note"],
                 encode_json(sections), encode_json(snapshot), self._digest_data(data), request_hash,
                 key, expires, now))
            connection.execute(
                "UPDATE referrals SET state='superseded',superseded_by=?,version=version+1 WHERE id=?",
                (new_referral_id, referral_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient["id"],
                               aggregate_type="referral", aggregate_id=new_referral_id, action="referral.refreshed",
                               occurred_at=now, payload={"supersedes": referral_id, "sections": sections,
                                                         "expires_at": expires})
            audit.append_event(connection, clinic_id=row["destination_clinic_id"], actor_id=None, patient_id=None,
                               aggregate_type="referral", aggregate_id=referral_id,
                               action="referral.superseded_at_source", occurred_at=now,
                               payload={"superseded_by": new_referral_id})
            fresh = connection.execute("SELECT * FROM referrals WHERE id=?", (new_referral_id,)).fetchone()
        return self._metadata(fresh)

    # ------------------------------------------------------------ 过期清扫

    def expire_referrals(self, clinic_id: str, actor_id: str, *, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValidationError("处理数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        sealed = 0
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), PERMISSION, clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT * FROM referrals WHERE sealed_at IS NULL AND state IN ('pending','accepted') "
                "AND (source_clinic_id=? OR destination_clinic_id=?) ORDER BY expires_at,id LIMIT ?",
                (clinic_id, clinic_id, limit)).fetchall()
            for row in rows:
                if self._seal_if_needed(connection, row, now, actor_id=actor_id):
                    sealed += 1
        return {"sealed": sealed, "as_of": now}

    # ------------------------------------------------------------ 内部规则

    @staticmethod
    def _validate_sections(sections: Any) -> list[str]:
        if not isinstance(sections, list) or not sections:
            raise ValidationError("至少选择一个授权资料章节")
        if len(sections) > len(REFERRAL_SECTIONS):
            raise ValidationError("资料章节数量超出限制")
        selected = [choice(item, "资料章节", REFERRAL_SECTIONS) for item in sections]
        normalized = sorted(set(selected))
        if len(normalized) != len(selected):
            raise ValidationError("资料章节不能重复")
        return normalized

    def _require_consent(self, connection, consent_id: str, patient_id: str, sections: list[str], now: str):
        consent = connection.execute(
            "SELECT * FROM consents WHERE id=? AND patient_id=? AND purpose='referral_disclosure'",
            (consent_id, patient_id)).fetchone()
        if consent is None:
            raise NotFound("转诊专项授权不存在")
        if consent["state"] != "granted":
            raise Conflict("转诊专项授权已撤回或失效")
        if consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now):
            raise Conflict("转诊专项授权已过期")
        scope = decode_json(consent["scope_json"]) if consent["scope_json"] else None
        if not isinstance(scope, list) or not set(sections) <= set(scope):
            raise Conflict("交接章节超出患者专项授权范围", details={"authorized_scope": scope})
        return consent

    def _seal_if_needed(self, connection, row, now: str, *, actor_id: str | None = None) -> bool:
        if row["sealed_at"] is not None:
            return False
        consent = connection.execute("SELECT state,expires_at FROM consents WHERE id=?", (row["consent_id"],)).fetchone()
        reason: str | None = None
        if consent is not None and consent["state"] == "withdrawn":
            reason = "consent_withdrawn"
        elif consent is None or consent["state"] != "granted":
            reason = "expired"
        elif consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now):
            reason = "expired"
        elif parsed_timestamp(row["expires_at"]) <= parsed_timestamp(now):
            reason = "expired"
        if reason is None:
            return False
        apply_seal(connection, row, now, reason, actor_id=actor_id)
        return True

    def _metadata(self, row, *, replayed: bool | None = None) -> dict[str, Any]:
        snapshot = decode_json(row["snapshot_json"])
        sealed = row["sealed_at"] is not None
        retained = [section for section in snapshot["sections"] if section in snapshot["data"]] if sealed else None
        result = {
            "id": row["id"], "source_clinic_id": row["source_clinic_id"],
            "destination_clinic_id": row["destination_clinic_id"], "patient_id": row["source_patient_id"],
            "purpose": row["purpose"], "note": row["note"], "sections": decode_json(row["sections_json"]),
            "state": row["state"], "designated_staff_id": row["designated_staff_id"],
            "consent_id": row["consent_id"], "expires_at": row["expires_at"], "created_at": row["created_at"],
            "responded_by": row["responded_by"], "responded_at": row["responded_at"],
            "decline_reason": row["decline_reason"], "sealed": sealed, "seal_reason": row["seal_reason"],
            "retained_sections": retained, "superseded_by": row["superseded_by"], "version": row["version"],
        }
        if replayed is not None:
            result["replayed"] = replayed
        return result

    @staticmethod
    def _digest_data(data: dict[str, Any]) -> str:
        return hashlib.sha256(encode_json(data).encode("utf-8")).hexdigest()

    def _retained_digest(self, row, retained: list[str]) -> str:
        snapshot = decode_json(row["snapshot_json"])
        return self._digest_data({k: snapshot["data"][k] for k in retained})

    # ---------------------------------------------------------- 快照白名单

    def _build_data(self, connection, patient, sections: list[str]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for section in sections:
            data[section] = self._section(connection, section, patient)
        return data

    @staticmethod
    def _section(connection, section: str, patient) -> Any:
        patient_id = patient["id"]
        if section == "profile":
            return {"patient_id": patient_id, "external_ref": patient["external_ref"],
                    "display_name": patient["display_name"], "birth_date": patient["birth_date"],
                    "state": patient["state"], "created_at": patient["created_at"]}
        if section == "assessments":
            rows = connection.execute(
                "SELECT id,kind,captured_at,captured_by,measurements_json,answers_json,source,status,signed_at,version "
                "FROM assessments WHERE patient_id=? ORDER BY captured_at,id", (patient_id,)).fetchall()
            return [{"id": r["id"], "kind": r["kind"], "captured_at": r["captured_at"],
                     "captured_by": r["captured_by"], "measurements": decode_json(r["measurements_json"]),
                     "answers": decode_json(r["answers_json"]), "source": r["source"],
                     "status": r["status"], "signed_at": r["signed_at"], "version": r["version"]} for r in rows]
        if section == "plans":
            rows = connection.execute(
                "SELECT id,kind,state,clinical_owner,assessment_id,consent_id,goal_json,risk_json,"
                "start_date,target_date,created_at,updated_at,version FROM plans WHERE patient_id=? ORDER BY created_at,id",
                (patient_id,)).fetchall()
            return [{"id": r["id"], "kind": r["kind"], "state": r["state"],
                     "clinical_owner": r["clinical_owner"], "assessment_id": r["assessment_id"],
                     "consent_id": r["consent_id"], "goal": decode_json(r["goal_json"]),
                     "risk": decode_json(r["risk_json"]), "start_date": r["start_date"],
                     "target_date": r["target_date"], "created_at": r["created_at"],
                     "updated_at": r["updated_at"], "version": r["version"]} for r in rows]
        if section == "observations":
            rows = connection.execute(
                "SELECT id,plan_id,kind,value_num,unit,observed_at,recorded_by,provenance,correction_of,created_at "
                "FROM observations WHERE patient_id=? ORDER BY observed_at,id", (patient_id,)).fetchall()
            return [dict(r) for r in rows]
        if section == "clinical_flags":
            rows = connection.execute(
                "SELECT id,category,severity,detail,state,effective_from,effective_until,reported_by,reviewed_by,"
                "reviewed_at,version FROM clinical_flags WHERE patient_id=? ORDER BY effective_from,id",
                (patient_id,)).fetchall()
            return [dict(r) for r in rows]
        if section == "incidents":
            rows = connection.execute(
                "SELECT id,plan_id,encounter_id,severity,state,category,onset_at,reported_at,reported_by,"
                "assigned_to,summary,version FROM incidents WHERE patient_id=? ORDER BY reported_at,id",
                (patient_id,)).fetchall()
            return [dict(r) for r in rows]
        if section == "followups":
            rows = connection.execute(
                "SELECT id,plan_id,due_at,channel,reason,state,assigned_to,outcome,created_at,version "
                "FROM followups WHERE patient_id=? ORDER BY due_at,id", (patient_id,)).fetchall()
            return [dict(r) for r in rows]
        if section == "encounters":
            encounters = connection.execute(
                "SELECT id,appointment_id,state,opened_by,opened_at,signed_by,signed_at,version "
                "FROM encounters WHERE patient_id=? AND state IN ('signed','amended') ORDER BY opened_at,id",
                (patient_id,)).fetchall()
            items = []
            for encounter in encounters:
                notes = connection.execute(
                    "SELECT n.section,n.body,n.author_id,n.revision,n.created_at FROM encounter_notes n "
                    "JOIN (SELECT section,MAX(revision) AS mr FROM encounter_notes WHERE encounter_id=? GROUP BY section) t "
                    "ON t.section=n.section AND t.mr=n.revision ORDER BY n.section",
                    (encounter["id"],)).fetchall()
                items.append({"id": encounter["id"], "state": encounter["state"],
                              "opened_at": encounter["opened_at"], "signed_by": encounter["signed_by"],
                              "signed_at": encounter["signed_at"], "version": encounter["version"],
                              "notes": [dict(n) for n in notes]})
            return items
        raise ValidationError("资料章节无效")


def apply_seal(connection, row, now: str, reason: str, *, actor_id: str | None) -> None:
    """封存交接：删除从未被读取的章节数据；已读取章节与全部审计记录保留。

    actor_id 只归属到其所属诊所一侧的审计事件，另一侧记为系统事件，
    与创建/答复时的双侧留痕方式保持一致。
    """
    snapshot = decode_json(row["snapshot_json"])
    accessed: set[str] = set()
    for access in connection.execute("SELECT sections_json FROM referral_access WHERE referral_id=?", (row["id"],)).fetchall():
        accessed.update(decode_json(access["sections_json"]))
    dropped = [section for section in snapshot["sections"] if section not in accessed]
    for section in dropped:
        snapshot["data"].pop(section, None)
    retained = [section for section in snapshot["sections"] if section in accessed]
    connection.execute(
        "UPDATE referrals SET snapshot_json=?,sealed_at=?,seal_reason=?,version=version+1 WHERE id=?",
        (encode_json(snapshot), now, reason, row["id"]))
    payload = {"reason": reason, "dropped_sections": dropped, "retained_sections": retained}
    audit.append_event(connection, clinic_id=row["destination_clinic_id"],
                       actor_id=actor_id if actor_id and _staff_in_clinic(connection, actor_id, row["destination_clinic_id"]) else None,
                       patient_id=None, aggregate_type="referral", aggregate_id=row["id"],
                       action="referral.sealed", occurred_at=now, payload=payload)
    audit.append_event(connection, clinic_id=row["source_clinic_id"],
                       actor_id=actor_id if actor_id and _staff_in_clinic(connection, actor_id, row["source_clinic_id"]) else None,
                       patient_id=row["source_patient_id"], aggregate_type="referral",
                       aggregate_id=row["id"], action="referral.sealed_at_source",
                       occurred_at=now, payload=payload)


def _staff_in_clinic(connection, staff_id: str, clinic_id: str) -> bool:
    return connection.execute("SELECT 1 FROM staff WHERE id=? AND clinic_id=?", (staff_id, clinic_id)).fetchone() is not None


def seal_referrals_for_consent(connection, consent, now: str, actor_id: str | None) -> int:
    """撤回专项授权时封存全部关联交接；未读章节立即不可继续访问。"""
    rows = connection.execute(
        "SELECT * FROM referrals WHERE consent_id=? AND sealed_at IS NULL AND state IN ('pending','accepted')",
        (consent["id"],)).fetchall()
    for row in rows:
        apply_seal(connection, row, now, "consent_withdrawn", actor_id=actor_id)
    return len(rows)
