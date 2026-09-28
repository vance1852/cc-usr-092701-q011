from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from careflow.clock import FrozenClock
from careflow.db import Database, decode_json
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.service import Careflow


class ReferralCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        first = self.app.initialize_clinic("澄序旗舰院", "Asia/Shanghai", "总院负责人", "LongPassphrase!2026")
        self.clinic_a = first["clinic_id"]
        self.owner_a = first["owner_id"]
        self.doctor_a = self.app.create_staff(self.clinic_a, "总院医生", "clinician", actor_id=self.owner_a)["id"]
        self.nurse_a = self.app.create_staff(self.clinic_a, "总院护士", "nurse", actor_id=self.owner_a)["id"]
        clinic_b = self.app.create_clinic("澄序评估中心", "Asia/Shanghai")
        self.clinic_b = clinic_b["id"]
        self.owner_b = self.app.create_staff(self.clinic_b, "分院负责人", "owner")["id"]
        self.doctor_b = self.app.create_staff(self.clinic_b, "分院医生", "clinician", actor_id=self.owner_b)["id"]
        self.other_doctor_b = self.app.create_staff(self.clinic_b, "分院另一位医生", "clinician", actor_id=self.owner_b)["id"]
        clinic_c = self.app.create_clinic("无关诊所", "UTC")
        self.clinic_c = clinic_c["id"]
        self.owner_c = self.app.create_staff(self.clinic_c, "外局负责人", "owner")["id"]
        self.patient = self.app.create_patient(self.clinic_a, self.owner_a, "xref-1", "林女士")["id"]
        assessment = self.app.create_assessment(self.clinic_a, self.doctor_a, self.patient, "weight",
                                                {"weight_kg": 72.5}, {"sleep": "一般"})
        self.app.sign_assessment(self.clinic_a, self.doctor_a, assessment["id"], expected_version=1)
        self.app.record_observation(self.clinic_a, self.doctor_a, self.patient, "weight_kg", 72.5,
                                    "2026-09-27T08:00:00+08:00")

    def tearDown(self):
        self.temp.cleanup()

    def consent(self, scope, *, revision=1, expires_at="2026-10-27T12:00:00Z"):
        digest = hashlib.sha256(f"referral-r{revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic_a, self.doctor_a, self.patient, "referral_disclosure",
                                      revision, digest, expires_at=expires_at, scope=scope)["id"]

    def create(self, sections=("profile", "assessments", "observations"), key=None, **kwargs):
        if "consent_id" in kwargs:
            consent_id = kwargs.pop("consent_id")
        else:
            scope = list(sections)
            consent_id = getattr(self, "_default_consent", None)
            if consent_id is None or not set(scope) <= set(self._default_consent_scope):
                consent_id = self.consent(scope)
                self._default_consent = consent_id
                self._default_consent_scope = scope
        return self.app.referrals.create_referral(
            self.clinic_a, self.doctor_a, self.patient, self.clinic_b, self.doctor_b,
            "further_assessment", list(sections), "2026-10-04T12:00:00Z", consent_id,
            note="需进一步评估体重管理方案", idempotency_key=key, **kwargs)

    def test_full_handshake_does_not_create_patient_at_destination(self):
        referral = self.create()
        incoming = self.app.referrals.list_referrals(self.clinic_b, self.doctor_b, "incoming")["items"]
        self.assertEqual([item["id"] for item in incoming], [referral["id"]])
        envelope = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertEqual(envelope["state"], "pending")
        self.assertNotIn("snapshot", envelope)
        # 接受前接收诊所没有、也看不到该患者档案。
        with self.assertRaises(NotFound):
            self.app.get_patient(self.clinic_b, self.doctor_b, self.patient)
        accepted = self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        self.assertEqual(accepted["state"], "accepted")
        viewed = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertIn("snapshot", viewed)
        self.assertEqual(viewed["snapshot"]["sections"], ["assessments", "observations", "profile"])
        self.assertEqual(viewed["snapshot"]["data"]["profile"]["display_name"], "林女士")
        self.assertEqual(viewed["snapshot"]["data"]["observations"][0]["value_num"], 72.5)
        # 接受与查看全过程均不产生接收诊所的在诊档案。
        with self.assertRaises(NotFound):
            self.app.get_patient(self.clinic_b, self.doctor_b, self.patient)

    def test_duplicate_send_returns_original_id(self):
        first = self.create(key="handoff-1")
        replay_by_key = self.create(key="handoff-1")
        self.assertEqual(replay_by_key["id"], first["id"])
        self.assertTrue(replay_by_key["replayed"])
        # 即使不带幂等键，相同目的/章节/期限/授权的重复发送也返回原编号。
        replay_by_content = self.create()
        self.assertEqual(replay_by_content["id"], first["id"])
        self.assertTrue(replay_by_content["replayed"])
        # 章节不同视为另一次交接。
        consent_id = self.consent(["profile", "assessments", "observations", "plans"], revision=2)
        other = self.create(sections=("profile", "assessments", "observations", "plans"), consent_id=consent_id)
        self.assertNotEqual(other["id"], first["id"])

    def test_declined_handshake_never_exposes_sections(self):
        referral = self.create()
        result = self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "decline", 1,
                                                     decline_reason="当前无对应专科出诊")
        self.assertEqual(result["state"], "declined")
        self.assertEqual(result["decline_reason"], "当前无对应专科出诊")
        viewed = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertNotIn("snapshot", viewed)
        with self.assertRaises(Conflict):
            self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 2)
        with self.assertRaises(Conflict):
            self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "decline", 1)

    def test_only_designated_clinician_may_respond_or_view(self):
        referral = self.create()
        with self.assertRaises(Forbidden):
            self.app.referrals.respond_referral(self.clinic_b, self.other_doctor_b, referral["id"], "accept", 1)
        with self.assertRaises(Forbidden):
            self.app.referrals.get_referral(self.clinic_b, self.other_doctor_b, referral["id"])
        # 分院负责人可以代为答复。
        accepted = self.app.referrals.respond_referral(self.clinic_b, self.owner_b, referral["id"], "accept", 1)
        self.assertEqual(accepted["state"], "accepted")

    def test_sections_must_stay_within_patient_authorization(self):
        consent_id = self.consent(["profile"])
        with self.assertRaises(Conflict):
            self.create(sections=("profile", "assessments"), consent_id=consent_id)
        with self.assertRaises(NotFound):
            self.app.referrals.create_referral(
                self.clinic_a, self.doctor_a, self.patient, self.clinic_b, self.doctor_b,
                "further_assessment", ["profile"], "2026-10-04T12:00:00Z", "cns_nonexistent")

    def test_referral_requires_special_purpose_consent_and_clinical_role(self):
        digest = hashlib.sha256(b"export").hexdigest()
        export_consent = self.app.grant_consent(self.clinic_a, self.doctor_a, self.patient, "data_export", 1, digest)["id"]
        with self.assertRaises(NotFound):
            self.app.referrals.create_referral(
                self.clinic_a, self.doctor_a, self.patient, self.clinic_b, self.doctor_b,
                "further_assessment", ["profile"], "2026-10-04T12:00:00Z", export_consent)
        # 护士岗位没有转诊管理权限。
        consent_id = self.consent(["profile"])
        with self.assertRaises(Forbidden):
            self.app.referrals.create_referral(
                self.clinic_a, self.nurse_a, self.patient, self.clinic_b, self.doctor_b,
                "further_assessment", ["profile"], "2026-10-04T12:00:00Z", consent_id)

    def test_withdrawal_seals_unread_sections_but_keeps_read_ones_and_audit(self):
        referral = self.create(sections=("profile", "assessments", "observations"))
        self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        # 第一次只打开（读取）全部章节。
        self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        consent_id = self.app.consent_history(self.clinic_a, self.doctor_a, self.patient,
                                              purpose="referral_disclosure")[0]["id"]
        outcome = self.app.withdraw_consent(self.clinic_a, self.doctor_a, consent_id, "患者撤回转诊授权")
        self.assertEqual(outcome["referrals_sealed"], 1)
        sealed = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertTrue(sealed["sealed"])
        self.assertEqual(sealed["seal_reason"], "consent_withdrawn")
        # 已读章节仍在快照中，未读章节在另一场景验证；此处三章均已读，应全部保留。
        self.assertEqual(sealed["snapshot"]["sections"], ["assessments", "observations", "profile"])

    def test_withdrawal_before_any_read_drops_everything_and_blocks_accept(self):
        referral = self.create(sections=("profile", "assessments", "observations"))
        consent_id = self.app.consent_history(self.clinic_a, self.doctor_a, self.patient,
                                              purpose="referral_disclosure")[0]["id"]
        self.app.withdraw_consent(self.clinic_a, self.doctor_a, consent_id, "患者改变主意")
        with self.assertRaises(Conflict):
            self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        pending = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertTrue(pending["sealed"])
        self.assertNotIn("snapshot", pending)
        # 双侧审计均保留封存记录。
        dest_events = self.app.audit_history(self.clinic_b, self.owner_b)
        self.assertIn("referral.sealed", {event["action"] for event in dest_events})
        source_events = self.app.audit_history(self.clinic_a, self.owner_a)
        self.assertIn("referral.sealed_at_source", {event["action"] for event in source_events})
        # 跨诊所写入后，两侧哈希链仍然完整。
        self.assertTrue(self.app.verify_audit(self.clinic_a, self.owner_a)["ok"])
        self.assertTrue(self.app.verify_audit(self.clinic_b, self.owner_b)["ok"])

    def test_partial_read_then_seal_removes_only_unread_sections(self):
        # 授权只允许两章，接受后通过底层访问模拟仅读过 profile。
        referral = self.create(sections=("profile", "observations"))
        self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO referral_access(id,referral_id,clinic_id,staff_id,sections_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                ("rac_seed", referral["id"], self.clinic_b, self.doctor_b, '["profile"]', "2026-09-27T13:00:00Z"))
        consent_id = self.app.consent_history(self.clinic_a, self.doctor_a, self.patient,
                                              purpose="referral_disclosure")[0]["id"]
        self.app.withdraw_consent(self.clinic_a, self.doctor_a, consent_id, "撤回")
        sealed = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertEqual(sealed["snapshot"]["sections"], ["profile"])
        self.assertNotIn("observations", sealed["snapshot"]["data"])
        with self.db.transaction(write=False) as connection:
            stored = decode_json(connection.execute("SELECT snapshot_json FROM referrals WHERE id=?",
                                                    (referral["id"],)).fetchone()["snapshot_json"])
            self.assertNotIn("observations", stored["data"])

    def test_expiry_is_enforced_at_boundary_and_by_sweep(self):
        referral = self.create()
        # 第二个交接使用更晚的有效期限，共用同一份仍有效的专项授权。
        consent_id = self.app.consent_history(self.clinic_a, self.doctor_a, self.patient,
                                              purpose="referral_disclosure")[0]["id"]
        second = self.app.referrals.create_referral(
            self.clinic_a, self.doctor_a, self.patient, self.clinic_b, self.doctor_b,
            "further_assessment", ["profile"], "2026-10-06T12:00:00Z", consent_id,
            idempotency_key="sweep-1")
        self.clock.set(datetime(2026, 10, 4, 12, 0, tzinfo=UTC))
        with self.assertRaises(Conflict):
            self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])  # 触发惰性封存
        self.assertTrue(self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])["sealed"])
        self.assertFalse(self.app.referrals.get_referral(self.clinic_a, self.doctor_a, second["id"])["sealed"])
        self.clock.set(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
        outcome = self.app.referrals.expire_referrals(self.clinic_a, self.doctor_a)
        self.assertEqual(outcome["sealed"], 1)
        self.assertTrue(self.app.referrals.get_referral(self.clinic_a, self.doctor_a, second["id"])["sealed"])

    def test_snapshot_is_frozen_and_source_changes_require_explicit_refresh(self):
        referral = self.create(sections=("profile", "observations"))
        self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        viewed = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertFalse(viewed["source_changed"])
        # 来源侧新增观察值：快照不静默扩大披露，只给出变化提示。
        self.app.record_observation(self.clinic_a, self.doctor_a, self.patient, "weight_kg", 71.9,
                                    "2026-09-28T08:00:00+08:00")
        changed = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.assertTrue(changed["source_changed"])
        self.assertEqual(len(changed["snapshot"]["data"]["observations"]), 1)
        # 重新获取产生新编号，旧交接被取代；新快照包含新值。
        renewed = self.app.referrals.refresh_referral(self.clinic_a, self.doctor_a, referral["id"],
                                                      "2026-10-11T12:00:00Z")
        self.assertNotEqual(renewed["id"], referral["id"])
        self.assertEqual(renewed["state"], "pending")
        old = self.app.referrals.get_referral(self.clinic_a, self.doctor_a, referral["id"])
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["superseded_by"], renewed["id"])
        with self.assertRaises(Conflict):
            self.app.referrals.refresh_referral(self.clinic_a, self.doctor_a, referral["id"],
                                                "2026-10-12T12:00:00Z")
        self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, renewed["id"], "accept", 1)
        fresh = self.app.referrals.get_referral(self.clinic_b, self.doctor_b, renewed["id"])
        self.assertEqual(len(fresh["snapshot"]["data"]["observations"]), 2)
        self.assertFalse(fresh["source_changed"])

    def test_unrelated_clinic_cannot_see_handshake(self):
        referral = self.create()
        with self.assertRaises(NotFound):
            self.app.referrals.get_referral(self.clinic_c, self.owner_c, referral["id"])
        with self.assertRaises(NotFound):
            self.app.referrals.respond_referral(self.clinic_c, self.owner_c, referral["id"], "accept", 1)
        self.assertEqual(self.app.referrals.list_referrals(self.clinic_c, self.owner_c, "incoming")["items"], [])

    def test_destination_must_name_active_clinician(self):
        consent_id = self.consent(["profile"])
        with self.assertRaises(ValidationError):
            # nurse_a 属于来源诊所，不能作为接收医生。
            self.app.referrals.create_referral(
                self.clinic_a, self.doctor_a, self.patient, self.clinic_b, self.nurse_a,
                "further_assessment", ["profile"], "2026-10-04T12:00:00Z", consent_id)

    def test_accepted_view_is_audited_per_access(self):
        referral = self.create(sections=("profile",))
        self.app.referrals.respond_referral(self.clinic_b, self.doctor_b, referral["id"], "accept", 1)
        self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        self.app.referrals.get_referral(self.clinic_b, self.doctor_b, referral["id"])
        events = [event for event in self.app.audit_history(self.clinic_b, self.owner_b)
                  if event["action"] == "referral.snapshot_accessed"]
        self.assertEqual(len(events), 2)
        # 来源侧同样可在患者时间线看到接收方的查看留痕。
        timeline = self.app.patient_timeline(self.clinic_a, self.doctor_a, self.patient)
        self.assertIn("referral.snapshot_viewed_at_destination",
                      {event["action"] for event in timeline["events"]})

    def test_diagnostics_flags_due_seal_after_expiry(self):
        referral = self.create()
        self.clock.set(datetime(2026, 10, 5, 0, 0, tzinfo=UTC))
        report = self.app.run_diagnostics(self.clinic_a, self.owner_a)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("referral.seal_due", codes)
        finding = next(f for f in report["findings"] if f["code"] == "referral.seal_due" and f["aggregate_id"] == referral["id"])
        self.assertEqual(finding["aggregate_type"], "referral")
        # 清扫封存后不再报告。
        self.app.referrals.expire_referrals(self.clinic_a, self.doctor_a)
        report_after = self.app.run_diagnostics(self.clinic_a, self.owner_a)
        self.assertNotIn("referral.seal_due", {f["code"] for f in report_after["findings"]})


if __name__ == "__main__":
    unittest.main()
