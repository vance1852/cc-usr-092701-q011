"""跨诊所转诊交接：目的限定、章节授权、冻结快照与可撤回访问。

交接在患者专项授权（purpose=referral_disclosure）与有效期内成立。接收医生
明确接受前，不会在接收诊所产生患者档案；接受后读取到的也只是发送时固化的
章节快照，来源记录后续变化只提示重新获取，不静默扩大披露。撤回或过期后
未读快照立即清除且无法继续访问，访问流水与双向哈希链审计永久保留。
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .exports import EXPORT_SECTIONS, PatientExportService
from .ids import new_id, require_id, require_idempotency_key
from .security import authorize, principal_for
from .validation import (
    choice,
    parsed_timestamp,
    request_digest,
    require_match,
    text,
    timestamp,
)

REFERRAL_PURPOSES = {"specialist_evaluation", "diagnostic_workup", "second_opinion", "continuity_of_care", "other"}
OPEN_STATES = ("offered", "accepted")
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 366 * 24 * 3600


class ReferralService:
    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ send

    def send(self, clinic_id: str, actor_id: str, destination_clinic_id: str, patient_id: str,
             purpose: str, sections: list[str], consent_id: str, expires_at: str,
             idempotency_key: str, *, purpose_detail: str | None = None) -> dict[str, Any]:
        destination_id = require_id(destination_clinic_id, "接收诊所编号")
        patient_id = require_id(patient_id, "患者编号")
        consent_id = require_id(consent_id, "专项授权编号")
        purpose = choice(purpose, "转诊目的", REFERRAL_PURPOSES)
        purpose_detail = text(purpose_detail, "目的说明", maximum=600) if purpose_detail else None
        sections = self._sections(sections)
        key = require_idempotency_key(idempotency_key)
        now = timestamp(self.clock.now())
        expires = timestamp(expires_at, "交接有效期限")
        ttl = (parsed_timestamp(expires) - parsed_timestamp(now)).total_seconds()
        if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
            raise ValidationError("交接有效期限必须在 1 分钟至 366 天之间")
        request = {"source_clinic_id": clinic_id, "destination_clinic_id": destination_id,
                    "patient_id": patient_id, "purpose": purpose, "purpose_detail": purpose_detail,
                    "sections": sections, "consent_id": consent_id, "expires_at": expires}
        request_hash = request_digest(request)
        referral_id = new_id("ref")
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "referral:send", clinic_id=clinic_id)
            old = connection.execute("SELECT * FROM referrals WHERE source_clinic_id=? AND idempotency_key=?",
                                      (clinic_id, key)).fetchone()
            if old:
                # 重复发送相同交接返回原编号；内容不同则拒绝复用幂等编号。
                if old["request_hash"] != request_hash:
                    raise Conflict("转诊幂等编号已用于其他交接内容")
                return self._summary(old, replayed=True)
            if destination_id == clinic_id:
                raise ValidationError("接收诊所必须是另一家诊所")
            destination = connection.execute("SELECT id,state FROM clinics WHERE id=?", (destination_id,)).fetchone()
            if destination is None:
                raise NotFound("接收诊所不存在")
            patient = connection.execute("SELECT * FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("只有在诊患者可以发起转诊")
            consent = connection.execute(
                "SELECT * FROM consents WHERE id=? AND patient_id=? AND purpose='referral_disclosure' AND state='granted'",
                (consent_id, patient_id)).fetchone()
            if consent is None:
                raise Conflict("转诊需要患者当前有效的专项披露授权")
            if consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now):
                raise Conflict("专项披露授权已过期")
            if consent["expires_at"] and parsed_timestamp(expires) > parsed_timestamp(consent["expires_at"]):
                raise Conflict("交接有效期不能晚于专项授权到期时间")
            snapshot, baseline = self._build_snapshot(connection, patient, sections, now)
            snapshot_json = encode_json(snapshot)
            snapshot_digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO referrals(id,source_clinic_id,destination_clinic_id,patient_id,consent_id,purpose,purpose_detail,"
                "sections_json,state,snapshot_json,snapshot_baseline_json,snapshot_digest,idempotency_key,request_hash,"
                "created_by,created_at,expires_at,version) "
                "VALUES(" + ",".join(["?"]*8) + ",'offered'," + ",".join(["?"]*8) + ",1)",
                (referral_id, clinic_id, destination_id, patient_id, consent_id, purpose, purpose_detail,
                 encode_json(sections), snapshot_json, encode_json(baseline), snapshot_digest, key, request_hash,
                 actor_id, now, expires))
            payload = {"destination_clinic_id": destination_id, "purpose": purpose, "sections": sections,
                       "expires_at": expires, "consent_id": consent_id, "snapshot_digest": snapshot_digest}
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.offered",
                               occurred_at=now, payload=payload)
            # 接收侧哈希链同步留痕；操作人尚非接收诊所员工，记为系统侧转交。
            audit.append_event(connection, clinic_id=destination_id, actor_id=None, patient_id=None,
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.received",
                               occurred_at=now, payload={**payload, "source_clinic_id": clinic_id})
            row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
        return self._summary(row, replayed=False)

    # --------------------------------------------------------------- respond

    def respond(self, clinic_id: str, actor_id: str, referral_id: str, decision: str,
                expected_version: int, *, assignee_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
        decision = choice(decision, "接收结论", {"accept", "decline"})
        referral_id = require_id(referral_id, "转诊编号")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "referral:respond", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("只有医生或诊所负责人可以给出接收结论")
            row = connection.execute("SELECT * FROM referrals WHERE id=? AND destination_clinic_id=?",
                                     (referral_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("转诊交接不存在")
            require_match(row["version"], expected_version, "转诊交接")
            if row["state"] != "offered":
                raise Conflict("只有待回应的交接可以接受或拒绝", details={"state": row["state"]})
            gate = self._gate_reason(connection, row, now)
            if gate is not None:
                # 到期或专项授权失效的交接不能再接受或拒绝；先提交终态落定，再拒绝操作。
                self._lapse_locked(connection, row, now, gate, actor_id=actor_id, via="respond_gate")
                connection.commit()
                raise Conflict("交接已到期或专项授权失效", details={"state": gate})
            if decision == "decline":
                reason = text(reason or "", "拒绝原因", maximum=1000)
                # 拒绝为终态：快照永不披露，按最小必要立即销毁，仅保留摘要与拒绝原因。
                self._clear_snapshot(connection, referral_id)
                connection.execute("UPDATE referrals SET state='declined',responded_by=?,responded_at=?,"
                                   "decline_reason=?,version=version+1 WHERE id=?",
                                   (actor_id, now, reason, referral_id))
                payload = {"reason": reason, "source_clinic_id": row["source_clinic_id"]}
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="referral", aggregate_id=referral_id, action="referral.declined",
                                   occurred_at=now, payload=payload)
                audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=None, patient_id=row["patient_id"],
                                   aggregate_type="referral", aggregate_id=referral_id, action="referral.declined",
                                   occurred_at=now, payload={**payload, "destination_clinic_id": clinic_id})
                return self._summary(connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone())
            assignee_id = require_id(assignee_id or actor_id, "接收医生编号")
            assignee = connection.execute("SELECT * FROM staff WHERE id=? AND clinic_id=? AND active=1",
                                          (assignee_id, clinic_id)).fetchone()
            if assignee is None or assignee["role"] not in {"clinician", "owner"}:
                raise ValidationError("接收医生必须是本诊所有效的医生或负责人")
            profile = decode_json(row["snapshot_json"])["data"].get("profile", {})
            accepted_patient_id = new_id("pat")
            # 接受瞬间才建立接收诊所的在诊档案；此前接收侧没有任何该患者的普通档案。
            connection.execute(
                "INSERT INTO patients(id,clinic_id,external_ref,display_name,birth_date,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?, 'active',?,?)",
                (accepted_patient_id, clinic_id, f"ref:{referral_id}", profile.get("display_name", "转诊患者"),
                 profile.get("birth_date"), now, now))
            connection.execute("UPDATE referrals SET state='accepted',assignee_id=?,responded_by=?,responded_at=?,"
                               "accepted_patient_id=?,version=version+1 WHERE id=?",
                               (assignee_id, actor_id, now, accepted_patient_id, referral_id))
            payload = {"assignee_id": assignee_id, "accepted_patient_id": accepted_patient_id,
                       "source_clinic_id": row["source_clinic_id"], "sections": decode_json(row["sections_json"])}
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=accepted_patient_id,
                               aggregate_type="patient", aggregate_id=accepted_patient_id, action="patient.created",
                               occurred_at=now, payload={"via_referral": referral_id, "external_ref": f"ref:{referral_id}"})
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=accepted_patient_id,
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.accepted",
                               occurred_at=now, payload=payload)
            audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=None, patient_id=row["patient_id"],
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.accepted",
                               occurred_at=now, payload={**payload, "destination_clinic_id": clinic_id})
            return self._summary(connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone())

    def revoke(self, clinic_id: str, actor_id: str, referral_id: str, reason: str) -> dict[str, Any]:
        """来源诊所在患者撤回授权或要求停止披露时主动吊销交接。"""
        referral_id = require_id(referral_id, "转诊编号")
        reason = text(reason, "吊销原因", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "referral:send", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM referrals WHERE id=? AND source_clinic_id=?",
                                      (referral_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("转诊交接不存在")
            result = self._revoke_locked(connection, row, now, actor_id=actor_id, reason=reason)
        return result

    def revoke_for_consent(self, connection, consent, now: str, actor_id: str, reason: str) -> int:
        """与授权撤回同一事务：专项授权失效即吊销全部相关交接。返回吊销数量。"""
        rows = connection.execute(
            "SELECT * FROM referrals WHERE consent_id=? AND state IN ('offered','accepted')",
            (consent["id"],)).fetchall()
        for row in rows:
            self._revoke_locked(connection, row, now, actor_id=actor_id, reason=f"专项授权撤回：{reason}")
        return len(rows)

    def lapse_for_replaced_consent(self, connection, previous_consent, now: str, actor_id: str) -> int:
        """旧版专项授权被新版本替代时，同事务令引用它的开放交接到期。"""
        rows = connection.execute(
            "SELECT * FROM referrals WHERE consent_id=? AND state IN ('offered','accepted')",
            (previous_consent["id"],)).fetchall()
        for row in rows:
            self._clear_snapshot(connection, row["id"])
            connection.execute("UPDATE referrals SET state='expired',version=version+1 WHERE id=?", (row["id"],))
            payload = {"lapsed_via": "consent_replaced", "previous_state": row["state"],
                       "consent_id": previous_consent["id"]}
            audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=actor_id,
                               patient_id=row["patient_id"], aggregate_type="referral", aggregate_id=row["id"],
                               action="referral.expired", occurred_at=now, payload=payload)
            audit.append_event(connection, clinic_id=row["destination_clinic_id"], actor_id=None,
                               patient_id=row["accepted_patient_id"], aggregate_type="referral",
                               aggregate_id=row["id"], action="referral.expired", occurred_at=now, payload=payload)
        return len(rows)

    def _revoke_locked(self, connection, row, now: str, *, actor_id: str | None, reason: str) -> dict[str, Any]:
        if row["state"] not in OPEN_STATES:
            return self._summary(row)
        self._clear_snapshot(connection, row["id"])
        connection.execute("UPDATE referrals SET state='revoked',revoked_at=?,version=version+1 WHERE id=?",
                           (now, row["id"]))
        payload = {"reason": reason, "previous_state": row["state"]}
        audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                           aggregate_type="referral", aggregate_id=row["id"], action="referral.revoked",
                           occurred_at=now, payload=payload)
        audit.append_event(connection, clinic_id=row["destination_clinic_id"], actor_id=None, patient_id=row["accepted_patient_id"],
                           aggregate_type="referral", aggregate_id=row["id"], action="referral.revoked",
                           occurred_at=now, payload={**payload, "source_clinic_id": row["source_clinic_id"]})
        return self._summary(connection.execute("SELECT * FROM referrals WHERE id=?", (row["id"],)).fetchone())

    def expire_due(self, clinic_id: str | None = None, *, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValidationError("处理数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        expired_ids: list[str] = []
        with self.db.transaction() as connection:
            if clinic_id:
                rows = connection.execute(
                    "SELECT * FROM referrals WHERE source_clinic_id=? AND state IN ('offered','accepted') AND expires_at<=? "
                    "ORDER BY expires_at,id LIMIT ?", (clinic_id, now, limit)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM referrals WHERE state IN ('offered','accepted') AND expires_at<=? "
                    "ORDER BY expires_at,id LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                self._clear_snapshot(connection, row["id"])
                connection.execute("UPDATE referrals SET state='expired',version=version+1 WHERE id=?", (row["id"],))
                payload = {"expires_at": row["expires_at"], "previous_state": row["state"]}
                audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=None, patient_id=row["patient_id"],
                                   aggregate_type="referral", aggregate_id=row["id"], action="referral.expired",
                                   occurred_at=now, payload=payload)
                audit.append_event(connection, clinic_id=row["destination_clinic_id"], actor_id=None,
                                   patient_id=row["accepted_patient_id"], aggregate_type="referral",
                                   aggregate_id=row["id"], action="referral.expired", occurred_at=now, payload=payload)
                expired_ids.append(row["id"])
        return {"expired": len(expired_ids), "referral_ids": expired_ids, "as_of": now}

    # ------------------------------------------------------------------ read

    def open_snapshot(self, clinic_id: str, actor_id: str, referral_id: str, *, sections: list[str] | None = None) -> dict[str, Any]:
        referral_id = require_id(referral_id, "转诊编号")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "referral:read", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM referrals WHERE id=? AND destination_clinic_id=?",
                                     (referral_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("转诊交接不存在")
            if row["state"] in {"revoked", "expired", "declined"} or row["snapshot_json"] is None:
                raise Conflict("交接资料已不可访问", details={"state": row["state"]})
            if row["state"] != "accepted":
                raise Conflict("交接尚未接受，不能查看资料", details={"state": row["state"]})
            if row["assignee_id"] != actor_id and principal.role != "owner":
                raise Forbidden("只有指定接收医生可以查看交接资料")
            gate = self._gate_reason(connection, row, now)
            if gate is not None:
                # 惰性把到期/失效落入终态（批量任务尚未跑到该交接时），提交后立即拒绝访问。
                self._lapse_locked(connection, row, now, gate, actor_id=actor_id, via="snapshot_gate")
                connection.commit()
                raise Conflict("交接资料已不可访问", details={"state": gate})
            authorized = decode_json(row["sections_json"])
            if sections is None:
                selected = authorized
            else:
                selected = self._sections(sections)
                extra = set(selected) - set(authorized)
                if extra:
                    raise ValidationError("请求章节超出患者授权范围", details={"sections": sorted(extra)})
            snapshot = decode_json(row["snapshot_json"])
            baseline = decode_json(row["snapshot_baseline_json"])
            patient = connection.execute("SELECT * FROM patients WHERE id=? AND clinic_id=?",
                                         (row["patient_id"], row["source_clinic_id"])).fetchone()
            changed_sections: list[str] = []
            if patient is None:
                changed_sections = list(authorized)
            else:
                for section in authorized:
                    if self._fingerprint(connection, patient, section) != baseline.get(section):
                        changed_sections.append(section)
            source_changed = bool(changed_sections)
            access_id = new_id("rac")
            connection.execute(
                "INSERT INTO referral_accesses(id,referral_id,clinic_id,patient_id,accessor_id,sections_json,"
                "snapshot_digest,source_changed,accessed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (access_id, referral_id, clinic_id, row["accepted_patient_id"], actor_id,
                 encode_json(selected), row["snapshot_digest"], 1 if source_changed else 0, now))
            data = {key: snapshot["data"][key]
                    for key in ("patient_id", "external_ref", "display_name", "state")}
            for section in selected:
                data[section] = snapshot["data"][section]
            result = {"referral_id": referral_id, "format": "careflow-referral-snapshot-v1",
                      "source_clinic_id": row["source_clinic_id"], "purpose": row["purpose"],
                      "captured_at": snapshot["captured_at"], "consent_id": row["consent_id"],
                      "expires_at": row["expires_at"], "sections": selected, "data": data,
                      "snapshot_digest": row["snapshot_digest"], "source_changed": source_changed,
                      "changed_sections": sorted(changed_sections),
                      "notice": "来源记录在快照后发生变化，章节已标注；如需新内容请由来源重新发起交接。" if source_changed else None,
                      "accessed_at": now}
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["accepted_patient_id"],
                               aggregate_type="referral", aggregate_id=referral_id, action="referral.snapshot_accessed",
                               occurred_at=now, payload={"sections": selected, "source_changed": source_changed,
                                                         "changed_sections": sorted(changed_sections),
                                                         "snapshot_digest": row["snapshot_digest"],
                                                         "access_id": access_id})
            return result

    def list_incoming(self, clinic_id: str, actor_id: str, *, state: str | None = None) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "referral:read", clinic_id=clinic_id)
            sql = "SELECT * FROM referrals WHERE destination_clinic_id=?"
            params: list[Any] = [clinic_id]
            if state:
                sql += " AND state=?"
                params.append(choice(state, "交接状态", {"offered", "accepted", "declined", "revoked", "expired"}))
            rows = connection.execute(sql + " ORDER BY created_at,id", params).fetchall()
            return {"clinic_id": clinic_id, "direction": "incoming", "items": [self._summary(row) for row in rows]}

    def list_outgoing(self, clinic_id: str, actor_id: str, *, state: str | None = None) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "referral:send", clinic_id=clinic_id)
            sql = "SELECT * FROM referrals WHERE source_clinic_id=?"
            params: list[Any] = [clinic_id]
            if state:
                sql += " AND state=?"
                params.append(choice(state, "交接状态", {"offered", "accepted", "declined", "revoked", "expired"}))
            rows = connection.execute(sql + " ORDER BY created_at,id", params).fetchall()
            return {"clinic_id": clinic_id, "direction": "outgoing", "items": [self._summary(row) for row in rows]}

    def get(self, clinic_id: str, actor_id: str, referral_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            row = self._owned_row(connection, clinic_id, actor_id, referral_id, write=False)
            return self._summary(row, include_access_count=True, connection=connection)

    def access_log(self, clinic_id: str, actor_id: str, referral_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            self._owned_row(connection, clinic_id, actor_id, referral_id, write=False)
            rows = connection.execute("SELECT * FROM referral_accesses WHERE referral_id=? ORDER BY accessed_at,id",
                                      (referral_id,)).fetchall()
            return {"referral_id": referral_id, "count": len(rows),
                    "accesses": [{"id": row["id"], "clinic_id": row["clinic_id"], "accessor_id": row["accessor_id"],
                                   "sections": decode_json(row["sections_json"]), "snapshot_digest": row["snapshot_digest"],
                                   "source_changed": bool(row["source_changed"]), "accessed_at": row["accessed_at"]}
                                  for row in rows]}

    # --------------------------------------------------------------- helpers

    def _owned_row(self, connection, clinic_id: str, actor_id: str, referral_id: str, *, write: bool):
        """来源诊所凭 referral:send、接收诊所凭 referral:read 查看交接元数据。"""
        row = connection.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
        if row is None:
            raise NotFound("转诊交接不存在")
        if row["destination_clinic_id"] == clinic_id:
            authorize(principal_for(connection, actor_id, clinic_id), "referral:read", clinic_id=clinic_id)
        elif row["source_clinic_id"] == clinic_id:
            authorize(principal_for(connection, actor_id, clinic_id), "referral:send", clinic_id=clinic_id)
        else:
            # 不向无关诊所泄露交接是否存在。
            raise NotFound("转诊交接不存在")
        return row

    @staticmethod
    def _sections(value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("至少指定一个授权章节")
        if len(value) > len(EXPORT_SECTIONS):
            raise ValidationError("授权章节数量超出限制")
        selected = [choice(item, "授权章节", EXPORT_SECTIONS) for item in value]
        if len(set(selected)) != len(selected):
            raise ValidationError("授权章节不能重复")
        return sorted(set(selected))

    @staticmethod
    def _build_snapshot(connection, patient, sections: list[str], now: str) -> tuple[dict[str, Any], dict[str, str]]:
        data: dict[str, Any] = {"patient_id": patient["id"], "external_ref": patient["external_ref"],
                                "display_name": patient["display_name"], "state": patient["state"]}
        baseline: dict[str, str] = {}
        for section in sections:
            projected = PatientExportService._section(connection, section, patient)
            data[section] = projected
            baseline[section] = ReferralService._digest(projected)
        snapshot = {"format": "careflow-referral-snapshot-v1", "source_clinic_id": patient["clinic_id"],
                    "captured_at": now, "sections": sections, "data": data}
        return snapshot, baseline

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()

    def _fingerprint(self, connection, patient, section: str) -> str:
        return self._digest(PatientExportService._section(connection, section, patient))

    def _gate_reason(self, connection, row, now: str) -> str | None:
        """授权撤回或到期/交接到期时返回应落入的终态名；否则返回 None。"""
        if parsed_timestamp(row["expires_at"]) <= parsed_timestamp(now):
            return "expired"
        consent = connection.execute("SELECT * FROM consents WHERE id=?", (row["consent_id"],)).fetchone()
        if consent is None or consent["state"] != "granted":
            return "revoked"
        if consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now):
            return "expired"
        return None

    def _lapse_locked(self, connection, row, now: str, terminal: str, *,
                       actor_id: str | None = None, via: str = "access_gate") -> None:
        self._clear_snapshot(connection, row["id"])
        connection.execute("UPDATE referrals SET state=?,version=version+1 WHERE id=?", (terminal, row["id"]))
        payload = {"lapsed_via": via, "previous_state": row["state"], "expires_at": row["expires_at"]}
        audit.append_event(connection, clinic_id=row["source_clinic_id"], actor_id=None, patient_id=row["patient_id"],
                           aggregate_type="referral", aggregate_id=row["id"], action=f"referral.{terminal}",
                           occurred_at=now, payload=payload)
        audit.append_event(connection, clinic_id=row["destination_clinic_id"], actor_id=actor_id,
                           patient_id=row["accepted_patient_id"], aggregate_type="referral",
                           aggregate_id=row["id"], action=f"referral.{terminal}", occurred_at=now, payload=payload)

    @staticmethod
    def _clear_snapshot(connection, referral_id: str) -> None:
        """吊销或过期即销毁快照内容；谁在何时看过哪些章节由访问流水与哈希链留证。"""
        connection.execute("UPDATE referrals SET snapshot_json=NULL WHERE id=? AND snapshot_json IS NOT NULL",
                           (referral_id,))

    def _summary(self, row, *, replayed: bool | None = None, include_access_count: bool = False,
                 connection=None) -> dict[str, Any]:
        result = {"id": row["id"], "source_clinic_id": row["source_clinic_id"],
                  "destination_clinic_id": row["destination_clinic_id"], "patient_id": row["patient_id"],
                  "accepted_patient_id": row["accepted_patient_id"], "consent_id": row["consent_id"],
                  "assignee_id": row["assignee_id"], "purpose": row["purpose"], "purpose_detail": row["purpose_detail"],
                  "sections": decode_json(row["sections_json"]), "state": row["state"],
                  "snapshot_available": row["snapshot_json"] is not None,
                  "snapshot_digest": row["snapshot_digest"], "created_by": row["created_by"],
                  "created_at": row["created_at"], "expires_at": row["expires_at"],
                  "responded_by": row["responded_by"], "responded_at": row["responded_at"],
                  "decline_reason": row["decline_reason"], "revoked_at": row["revoked_at"],
                  "version": row["version"]}
        if replayed is not None:
            result["replayed"] = replayed
        if include_access_count and connection is not None:
            result["access_count"] = connection.execute(
                "SELECT count(*) FROM referral_accesses WHERE referral_id=?", (row["id"],)).fetchone()[0]
        return result
