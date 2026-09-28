from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database, decode_json
from careflow.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationError
from careflow.service import Careflow


class ReferralCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinics.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        first = self.app.initialize_clinic("澄序总院", "Asia/Shanghai", "总院负责人", "LongPassphrase!2026")
        self.clinic_a = first["clinic_id"]
        self.owner_a = first["owner_id"]
        self.doctor_a = self.app.create_staff(self.clinic_a, "来源医生", "clinician", actor_id=self.owner_a)["id"]
        self.nurse_a = self.app.create_staff(self.clinic_a, "总院护理", "nurse", actor_id=self.owner_a)["id"]
        second = self.app.create_clinic("澄序分院", "Asia/Shanghai")
        self.clinic_b = second["id"]
        self.owner_b = self.app.create_staff(self.clinic_b, "分院负责人", "owner")["id"]
        self.doctor_b = self.app.create_staff(self.clinic_b, "接收医生", "clinician", actor_id=self.owner_b)["id"]
        self.nurse_b = self.app.create_staff(self.clinic_b, "分院护理", "nurse", actor_id=self.owner_b)["id"]
        self._consent_seq = 0
        self._default_consent = None
        self.patient = self.app.create_patient(self.clinic_a, self.owner_a, "x-001", "林女士",
                                                birth_date="1990-05-06", phone_ciphertext="enc::phone::blob")
        self._seed_records()

    def tearDown(self):
        self.temp.cleanup()

    def consent(self, *, expires_at=None):
        self._consent_seq += 1
        revision = self._consent_seq
        digest = hashlib.sha256(f"referral-r{revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic_a, self.doctor_a, self.patient["id"],
                                      "referral_disclosure", revision, digest, expires_at=expires_at)

    def default_consent(self):
        if self._default_consent is None:
            self._default_consent = self.consent()
        return self._default_consent

    def _seed_records(self):
        self.asm = self.app.create_assessment(
            self.clinic_a, self.doctor_a, self.patient["id"], "weight",
            {"weight_kg": "82.0", "waist_cm": 91}, {"goal": "复核代谢指标"})
        self.app.sign_assessment(self.clinic_a, self.doctor_a, self.asm["id"], expected_version=1)
        # 与本次转诊无关的医美评估：不在授权章节内时绝不能出现。
        aesthetic = self.app.create_assessment(
            self.clinic_a, self.doctor_a, self.patient["id"], "aesthetic", {}, {"area": "面部咨询"})
        self.app.sign_assessment(self.clinic_a, self.doctor_a, aesthetic["id"], expected_version=1)
        self.obs1 = self.app.record_observation(
            self.clinic_a, self.doctor_a, self.patient["id"], "weight_kg", 82.0,
            "2026-09-20T08:00:00+08:00")
        return aesthetic

    def send(self, *, sections=None, expires_at="2026-10-04T12:00:00Z", key="ref-key-1", **kwargs):
        consent = kwargs.pop("consent", None) or self.default_consent()
        return self.app.referrals.send(
            self.clinic_a, kwargs.pop("actor", self.doctor_a), self.clinic_b, self.patient["id"],
            kwargs.pop("purpose", "specialist_evaluation"),
            sections if sections is not None else ["profile", "observations"],
            consent["id"], expires_at, key,
            purpose_detail=kwargs.pop("purpose_detail", "体重管理需要进一步评估"))

    def accept(self, referral_id, *, actor=None, assignee=None, expected_version=1):
        return self.app.referrals.respond(
            self.clinic_b, actor or self.doctor_b, referral_id, "accept", expected_version,
            assignee_id=assignee)

    # ------------------------------------------------------------ 发起与授权

    def test_send_requires_dedicated_consent_and_respects_expiry_bounds(self):
        with self.assertRaises(Conflict):
            self.send(consent={"id": "cns_00000000000000000000000000000000"})
        granted = self.consent(expires_at="2026-09-28T12:00:00Z")
        with self.assertRaises(Conflict):
            self.send(consent=granted, expires_at="2026-10-01T12:00:00Z", key="ref-too-long")
        with self.assertRaises(ValidationError):
            self.send(expires_at="2026-09-27T12:00:30Z", key="ref-past")
        unknown_purpose_consent = self._other_purpose_consent()
        with self.assertRaises(Conflict):
            self.send(consent=unknown_purpose_consent, key="ref-wrong-purpose")
        # 格式合法但不存在的专项授权编号同样拒绝。
        with self.assertRaises((Conflict, NotFound)):
            self.app.referrals.send(self.clinic_a, self.doctor_a, self.clinic_b, self.patient["id"],
                                     "specialist_evaluation", ["profile"], "cns_unknown",
                                     "2026-10-04T12:00:00Z", "ref-no-consent")

    def _other_purpose_consent(self):
        digest = hashlib.sha256(b"export").hexdigest()
        return self.app.grant_consent(self.clinic_a, self.doctor_a, self.patient["id"],
                                     "data_export", 1, digest)

    def test_send_validates_sections_purpose_and_destination(self):
        granted = self.consent()
        with self.assertRaises(ValidationError):
            self.send(sections=[], consent=granted, key="k1")
        with self.assertRaises(ValidationError):
            self.send(sections=["profile", "profile"], consent=granted, key="k2")
        with self.assertRaises(ValidationError):
            self.send(sections=["profile", "phone_records"], consent=granted, key="k3")
        with self.assertRaises(NotFound):
            self.app.referrals.send(self.clinic_a, self.doctor_a, "cln_unknown", self.patient["id"],
                                    "second_opinion", ["profile"], granted["id"],
                                    "2026-10-04T12:00:00Z", "k4")
        with self.assertRaises(ValidationError):
            self.app.referrals.send(self.clinic_a, self.doctor_a, self.clinic_a, self.patient["id"],
                                    "second_opinion", ["profile"], granted["id"],
                                    "2026-10-04T12:00:00Z", "k5")

    def test_only_clinical_roles_can_send_and_destination_roles_are_enforced(self):
        with self.assertRaises(Forbidden):
            self.send(actor=self.nurse_a, key="nurse-send")
        with self.assertRaises(Forbidden):
            self.app.referrals.respond(self.clinic_b, self.nurse_b, "ref_anything", "accept", 1)

    def test_duplicate_send_returns_original_id_but_different_content_conflicts(self):
        first = self.send(key="dup-1")
        replay = self.send(key="dup-1")
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            self.send(key="dup-1", sections=["profile", "assessments"])

    def test_snapshot_is_minimized_and_contains_no_contact_ciphertext(self):
        sent = self.send(sections=["profile", "observations"])
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT snapshot_json,sections_json FROM referrals WHERE id=?", (sent["id"],)).fetchone()
            snapshot = decode_json(row["snapshot_json"])
        self.assertNotIn("phone_ciphertext", row["snapshot_json"])
        self.assertEqual(set(snapshot["data"]),
                         {"patient_id", "external_ref", "display_name", "state", "profile", "observations"})
        # 无关医美评估不在授权章节，快照中不存在评估章节。
        self.assertNotIn("assessments", snapshot["data"])
        self.assertEqual([item["value_num"] for item in snapshot["data"]["observations"]], [82.0])

    # ------------------------------------------------------------ 接受/拒绝

    def test_no_patient_record_exists_at_destination_before_acceptance(self):
        sent = self.send()
        with self.assertRaises(NotFound):
            self.app.get_patient(self.clinic_b, self.doctor_b, sent["patient_id"])
        with self.db.transaction(write=False) as conn:
            count = conn.execute("SELECT count(*) FROM patients WHERE clinic_id=?", (self.clinic_b,)).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(sent["state"], "offered")
        self.assertIsNone(sent["accepted_patient_id"])

    def test_accept_creates_destination_record_and_assigns_doctor(self):
        sent = self.send()
        accepted = self.accept(sent["id"])
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["assignee_id"], self.doctor_b)
        new_patient_id = accepted["accepted_patient_id"]
        record = self.app.get_patient(self.clinic_b, self.doctor_b, new_patient_id)
        self.assertEqual(record["display_name"], "林女士")
        self.assertEqual(record["external_ref"], f"ref:{sent['id']}")
        with self.assertRaises(Conflict):
            self.accept(sent["id"], expected_version=2)
        history = self.app.audit_history(self.clinic_b, self.doctor_b, patient_id=new_patient_id)
        self.assertEqual([event["action"] for event in history], ["patient.created", "referral.accepted"])

    def test_decline_requires_reason_and_creates_no_record(self):
        sent = self.send(key="decline-1")
        with self.assertRaises(ValidationError):
            self.app.referrals.respond(self.clinic_b, self.doctor_b, sent["id"], "decline", 1)
        declined = self.app.referrals.respond(self.clinic_b, self.doctor_b, sent["id"], "decline", 1,
                                              reason="本院暂不具备该评估能力")
        self.assertEqual(declined["state"], "declined")
        self.assertEqual(declined["decline_reason"], "本院暂不具备该评估能力")
        with self.db.transaction(write=False) as conn:
            count = conn.execute("SELECT count(*) FROM patients WHERE clinic_id=?", (self.clinic_b,)).fetchone()[0]
        self.assertEqual(count, 0)
        with self.assertRaises(Conflict):
            self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])

    def test_accept_rejects_unknown_or_cross_clinic_referral(self):
        with self.assertRaises(NotFound):
            self.accept("ref_unknown")
        sent = self.send(key="boundary-1")
        # 来源医生不能以接收诊所身份回应。
        with self.assertRaises(Unauthorized):
            self.app.referrals.respond(self.clinic_b, self.doctor_a, sent["id"], "accept", 1)

    def test_expired_or_consent_lapsed_offer_can_no_longer_be_accepted(self):
        granted = self.consent(expires_at="2026-09-28T00:00:00Z")
        sent = self.send(consent=granted, expires_at="2026-09-27T13:00:00Z", key="short-1")
        self.clock.set(datetime(2026, 9, 27, 13, 5, tzinfo=UTC))
        with self.assertRaises(Conflict):
            self.accept(sent["id"])
        self.assertEqual(self.app.referrals.get(self.clinic_a, self.doctor_a, sent["id"])["state"], "expired")

    # ------------------------------------------------------------ 快照读取

    def test_snapshot_only_readable_after_acceptance_and_by_assignee(self):
        sent = self.send()
        with self.assertRaises(Conflict):
            self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        self.accept(sent["id"])
        other = self.app.create_staff(self.clinic_b, "另一名医生", "clinician", actor_id=self.owner_b)["id"]
        with self.assertRaises(Forbidden):
            self.app.referrals.open_snapshot(self.clinic_b, other, sent["id"])
        opened = self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        self.assertEqual(opened["sections"], ["observations", "profile"])
        self.assertFalse(opened["source_changed"])
        # 负责人可以代为查看。
        self.assertTrue(self.app.referrals.open_snapshot(self.clinic_b, self.owner_b, sent["id"]))

    def test_requested_sections_cannot_exceed_authorization(self):
        sent = self.send(sections=["profile", "observations"])
        self.accept(sent["id"])
        with self.assertRaises(ValidationError):
            self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"],
                                              sections=["profile", "plans"])
        subset = self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"],
                                                  sections=["profile"])
        self.assertEqual(set(subset["data"]), {"patient_id", "external_ref", "display_name", "state", "profile"})

    def test_source_changes_after_send_flag_sections_but_never_silently_expand_snapshot(self):
        sent = self.send(sections=["profile", "observations"])
        self.accept(sent["id"])
        before = self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        self.assertFalse(before["source_changed"])
        self.app.record_observation(self.clinic_a, self.doctor_a, self.patient["id"], "weight_kg", 80.4,
                                   "2026-09-25T08:00:00+08:00")
        # 推进时钟，使变更后的访问与首次访问在审计流水中可稳定区分先后。
        self.clock.set(datetime(2026, 9, 27, 13, 0, tzinfo=UTC))
        opened = self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        self.assertTrue(opened["source_changed"])
        self.assertEqual(opened["changed_sections"], ["observations"])
        self.assertIn("重新发起交接", opened["notice"])
        # 披露的仍是冻结快照：新观察值没有静默进入。
        self.assertEqual([item["value_num"] for item in opened["data"]["observations"]], [82.0])
        self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        log = self.app.referrals.access_log(self.clinic_b, self.doctor_b, sent["id"])
        self.assertEqual(log["count"], 3)
        self.assertFalse(log["accesses"][0]["source_changed"])
        self.assertTrue(all(item["source_changed"] for item in log["accesses"][1:]))

    # ------------------------------------------------------------ 撤回与过期

    def test_consent_withdrawal_revokes_referrals_and_blocks_unread_data(self):
        granted = self.consent()
        sent = self.send(consent=granted, key="withdraw-1")
        self.accept(sent["id"])
        self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        result = self.app.withdraw_consent(self.clinic_a, self.doctor_a, granted["id"], "患者改变主意")
        self.assertEqual(result["referrals_revoked"], 1)
        summary = self.app.referrals.get(self.clinic_b, self.doctor_b, sent["id"])
        self.assertEqual(summary["state"], "revoked")
        self.assertFalse(summary["snapshot_available"])
        with self.assertRaises(Conflict):
            self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        with self.db.transaction(write=False) as conn:
            self.assertIsNone(conn.execute("SELECT snapshot_json FROM referrals WHERE id=?", (sent["id"],)).fetchone()[0])
        # 已产生的访问流水仍然保留。
        log = self.app.referrals.access_log(self.clinic_b, self.owner_b, sent["id"])
        self.assertEqual(log["count"], 1)

    def test_replacing_consent_with_a_new_revision_lapses_referrals_too(self):
        first = self.consent()
        sent = self.send(consent=first, expires_at="2026-12-01T12:00:00Z", key="replaced-1")
        self.accept(sent["id"])
        self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        second = self.consent()  # 更高版本，旧授权被置为 expired
        self.assertEqual(self.app.referrals.get(self.clinic_a, self.doctor_a, sent["id"])["state"], "expired")
        with self.assertRaises(Conflict):
            self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        # 引用旧授权的到期交接不会被新授权恢复访问。
        fresh = self.send(consent=second, expires_at="2026-12-20T12:00:00Z", key="replaced-2")
        self.assertEqual(fresh["state"], "offered")

    def test_batch_expiry_clears_snapshots_but_keeps_access_audit(self):
        granted = self.consent(expires_at="2026-10-10T12:00:00Z")
        first = self.send(consent=granted, expires_at="2026-09-27T12:30:00Z", key="exp-1")
        second = self.send(consent=granted, expires_at="2026-09-27T13:30:00Z", key="exp-2")
        self.accept(first["id"])
        self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, first["id"])
        self.clock.set(datetime(2026, 9, 27, 13, 0, tzinfo=UTC))
        outcome = self.app.referrals.expire_due(self.clinic_a)
        self.assertEqual(outcome["expired"], 1)
        self.assertEqual(outcome["referral_ids"], [first["id"]])
        with self.assertRaises(Conflict):
            self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, first["id"])
        # 尚未到期的第二份不受影响。
        self.assertEqual(self.app.referrals.get(self.clinic_b, self.doctor_b, second["id"])["state"], "offered")
        log = self.app.referrals.access_log(self.clinic_b, self.doctor_b, first["id"])
        self.assertEqual(log["accesses"][0]["snapshot_digest"], first["snapshot_digest"])

    def test_source_can_manually_revoke_and_revoking_again_is_stable(self):
        sent = self.send(key="manual-revoke")
        revoked = self.app.referrals.revoke(self.clinic_a, self.doctor_a, sent["id"], "患者口头要求停止")
        self.assertEqual(revoked["state"], "revoked")
        again = self.app.referrals.revoke(self.clinic_a, self.doctor_a, sent["id"], "重复操作")
        self.assertEqual(again["state"], "revoked")
        # 接收诊所账号不属于来源诊所，按统一诊所隔离规则拒绝、不泄露交接是否存在。
        with self.assertRaises(Unauthorized):
            self.app.referrals.revoke(self.clinic_a, self.doctor_b, sent["id"], "接收方无权吊销")

    # ------------------------------------------------------------ 列表与审计

    def test_incoming_and_outgoing_lists_and_clinic_isolation(self):
        first = self.send(key="list-1")
        self.accept(first["id"])
        second = self.send(key="list-2", purpose="second_opinion")
        incoming = self.app.referrals.list_incoming(self.clinic_b, self.doctor_b)
        self.assertEqual({item["id"] for item in incoming["items"]}, {first["id"], second["id"]})
        offered = self.app.referrals.list_incoming(self.clinic_b, self.doctor_b, state="offered")
        self.assertEqual([item["id"] for item in offered["items"]], [second["id"]])
        outgoing = self.app.referrals.list_outgoing(self.clinic_a, self.doctor_a)
        self.assertEqual(len(outgoing["items"]), 2)
        # 列表元数据不含快照正文（只有摘要与是否可访问标志）。
        for collection in (incoming, outgoing):
            for item in collection["items"]:
                self.assertNotIn("snapshot_json", item)
                self.assertNotIn("data", item)
        self.assertIn("snapshot_digest", incoming["items"][0])
        # 无关诊所既看不到来源也看不到接收清单。
        third = self.app.create_clinic("第三方诊所", "UTC")
        outsider = self.app.create_staff(third["id"], "负责人", "owner")["id"]
        with self.assertRaises(NotFound):
            self.app.referrals.get(third["id"], outsider, first["id"])

    def test_dual_clinic_audit_chains_remain_intact_and_retention_survives(self):
        granted = self.consent()
        sent = self.send(consent=granted, key="audit-1")
        accepted = self.accept(sent["id"])
        self.app.referrals.open_snapshot(self.clinic_b, self.doctor_b, sent["id"])
        self.app.withdraw_consent(self.clinic_a, self.doctor_a, granted["id"], "撤回")
        self.assertTrue(self.app.verify_audit(self.clinic_a, self.owner_a)["ok"])
        self.assertTrue(self.app.verify_audit(self.clinic_b, self.owner_b)["ok"])
        events_a = {event["action"] for event in self.app.audit_history(self.clinic_a, self.owner_a)}
        events_b = {event["action"] for event in self.app.audit_history(self.clinic_b, self.owner_b)}
        patient_events = {event["action"] for event in
                          self.app.audit_history(self.clinic_b, self.doctor_b, patient_id=accepted["accepted_patient_id"])}
        self.assertIn("referral.offered", events_a)
        self.assertIn("referral.revoked", events_a)
        self.assertIn("referral.received", events_b)
        self.assertTrue({"referral.accepted", "referral.snapshot_accessed",
                          "referral.revoked", "patient.created"} <= patient_events)

    # ------------------------------------------------------------ HTTP 端到端

    def test_http_referral_happy_path(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            token_a = self._login(base, self.clinic_a, self.owner_a, "LongPassphrase!2026")
            # 分院负责人初始没有密码，先设置再登录。
            self.app.set_password(self.clinic_b, self.owner_b, self.owner_b, "BranchPass!2026")
            token_b = self._login(base, self.clinic_b, self.owner_b, "BranchPass!2026")
            granted = self.default_consent()
            body = {"destination_clinic_id": self.clinic_b, "patient_id": self.patient["id"],
                    "purpose": "specialist_evaluation", "sections": ["profile", "observations"],
                    "consent_id": granted["id"], "expires_at": "2026-10-04T12:00:00Z",
                    "purpose_detail": "体重管理进一步评估"}
            request = Request(base + "/referrals", data=json.dumps(body).encode(), method="POST",
                              headers={"X-Clinic-ID": self.clinic_a, "Authorization": f"Bearer {token_a}",
                                       "Content-Type": "application/json", "Idempotency-Key": "http-ref-1"})
            with urlopen(request, timeout=3) as response:
                sent = json.loads(response.read())
                self.assertEqual(response.status, 201)
            request = Request(base + "/referrals/incoming",
                              headers={"X-Clinic-ID": self.clinic_b, "Authorization": f"Bearer {token_b}"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(len(json.loads(response.read())["items"]), 1)
            respond = Request(base + f"/referrals/{sent['id']}/respond",
                              data=json.dumps({"decision": "accept", "expected_version": 1,
                                               "assignee_id": self.doctor_b}).encode(),
                              method="POST",
                              headers={"X-Clinic-ID": self.clinic_b, "Authorization": f"Bearer {token_b}",
                                       "Content-Type": "application/json"})
            with urlopen(respond, timeout=3) as response:
                self.assertEqual(response.status, 200)
            snapshot = Request(base + f"/referrals/{sent['id']}/snapshot",
                               headers={"X-Clinic-ID": self.clinic_b, "Authorization": f"Bearer {token_b}"})
            with urlopen(snapshot, timeout=3) as response:
                opened = json.loads(response.read())
                self.assertEqual(opened["sections"], ["observations", "profile"])
            bad = Request(base + f"/referrals/{sent['id']}/snapshot",
                          headers={"X-Clinic-ID": self.clinic_a, "Authorization": f"Bearer {token_a}"})
            with self.assertRaises(HTTPError) as error:
                urlopen(bad, timeout=3)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def _login(self, base, clinic_id, staff_id, password):
        request = Request(base + "/auth/token", data=json.dumps({"staff_id": staff_id, "password": password}).encode(),
                          method="POST", headers={"X-Clinic-ID": clinic_id, "Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read())["access_token"]


if __name__ == "__main__":
    unittest.main()
