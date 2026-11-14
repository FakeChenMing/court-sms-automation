import importlib.util
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("party_context", Path(__file__).resolve().parents[1] / "scripts/party_context.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class PartyContextTests(unittest.TestCase):
    CASE = "（2026）测0114民初90001号"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "messages.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY,text TEXT,attributedBody BLOB,date INTEGER,is_from_me INTEGER)")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def insert(self, rowid, text, date="2026-09-01T12:00:00+08:00", attributed=False, outgoing=False):
        raw = int((datetime.fromisoformat(date) - M.APPLE_EPOCH).total_seconds() * 1_000_000_000)
        blob = b"\x00NSString\x01\x00" + text.encode() + b"\x02other_metadata" if attributed else None
        self.conn.execute("INSERT INTO message VALUES(?,?,?,?,?)", (rowid, None if attributed else text, blob, raw, int(outgoing)))
        self.conn.commit()

    def test_history_includes_attributed_body_before_state_window(self):
        self.insert(3, "【法院】(2026) 测0114 民初90001号：原告甲公司，被告乙公司", attributed=True)
        state = {"message_jobs": {"7": {"message": {"rowid": 7, "text": self.CASE + "后续通知"}, "received_at": "2026-11-02"}}}
        result = M.collect_case_messages(self.CASE, state, [], self.db)
        self.assertEqual([m["id"] for m in result["messages"]], ["3", "7"])
        self.assertEqual(result["source_status"]["added_historical_messages"], 1)

    def test_same_tail_other_year_court_phone_and_outgoing_are_excluded(self):
        self.insert(1, "【法院】（2025）测0114民初90001号：乙公司")
        self.insert(2, "【法院】（2026）测0113民初90001号：丙公司")
        self.insert(3, "【电网】联系电话129000100")
        self.insert(4, self.CASE, outgoing=True)
        self.insert(5, self.CASE, date="2025-12-31T23:59:59+08:00")
        self.assertEqual(M.collect_case_messages(self.CASE, {}, [], self.db)["messages"], [])

    def test_fullwidth_digits_and_parentheses_are_normalized(self):
        self.insert(1, "【法院】（２０２６）测０１１４民初９０００１号：原告甲公司", attributed=True)
        self.assertEqual(len(M.collect_case_messages(self.CASE, {}, [], self.db)["messages"]), 1)

    def test_receive_year_not_case_year_and_links_redacted(self):
        case = "（2025）测0114民初90001号"
        self.insert(1, "【法院】" + case + "：https://example.invalid/secret?token=hidden")
        messages = M.collect_case_messages(case, {}, [], self.db)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("token", messages[0]["text"])

    def test_duplicate_rowid_has_two_sources_not_two_messages(self):
        self.insert(1, self.CASE + "原告甲公司")
        state = {"message_jobs": {"1": {"message": {"rowid": 1, "text": self.CASE + "原告甲公司"}, "received_at": "2026-09-01T12:00:00+08:00"}}}
        result = M.collect_case_messages(self.CASE, state, [], self.db)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(len(result["messages"][0]["context_sources"]), 2)

    def test_linked_delivery_without_case_is_retained(self):
        state = {"message_jobs": {"1": {"message": {"rowid": 1, "text": "https://example.invalid/secret"}, "received_at": "2026-09-01"}}}
        docs = [{"case_number": self.CASE, "deliveries": [{"source_rowid": 1}]}]
        self.assertEqual(len(M.collect_case_messages(self.CASE, state, docs)["messages"]), 1)

    def test_missing_database_is_visible_and_not_created(self):
        missing = Path(self.temp.name) / "missing.db"
        result = M.collect_case_messages(self.CASE, {}, [], missing)
        self.assertEqual(result["source_status"]["messages_database"], "unavailable")
        self.assertTrue(result["errors"])
        self.assertFalse(missing.exists())

    def test_greeting_is_not_party_even_when_role_unknown(self):
        source = "【人民法院】律师甲你好，请查收" + self.CASE + "案件中的文书。"
        p = {"name": "律师甲", "role": "当事人（角色未明）", "source_message_id": "1", "source_quote": source}
        self.assertIn("收件人", M.party_claim_issue(p, source))
        source = source.replace("律师甲你好", "律师甲律师你好")
        p["source_quote"] = source
        self.assertIn("收件人", M.party_claim_issue(p, source))

    def test_greeting_with_independent_party_identification_can_be_used(self):
        source = "【法院】张三你好，原告张三与被告乙公司的案件" + self.CASE
        p = {"name": "张三", "role": "原告", "source_message_id": "1", "source_quote": "原告张三与被告乙公司的案件"}
        self.assertIsNone(M.party_claim_issue(p, source))

    def test_applicant_not_guessed_as_plaintiff(self):
        source = "申请方：甲公司；被申请方：乙公司"
        p = {"name": "甲公司", "role": "原告", "source_quote": source}
        self.assertIsNotNone(M.party_claim_issue(p, source))
        p["role"] = "申请方"
        self.assertIsNone(M.party_claim_issue(p, source))
        p["role"] = "原告（依文书表述顺序推定）"
        self.assertIsNotNone(M.party_claim_issue(p, source))

    def test_response_notice_sues_you_identifies_multiple_parties(self):
        source = "示例市人民法院\n应诉通知书\n" + self.CASE + "\n王甲、许乙：\n本院已经受理示例甲公司、示例乙公司诉你侵害经营秘密纠纷一案。"
        for name, role, quote in [("示例甲公司", "原告", "示例甲公司、示例乙公司诉你"),
                                  ("示例乙公司", "原告", "示例甲公司、示例乙公司诉你"),
                                  ("王甲", "被告", "王甲、许乙："),
                                  ("许乙", "被告", "王甲、许乙：")]:
            self.assertIsNone(M.party_claim_issue({"name": name, "role": role, "source_quote": quote}, source))
        self.assertIsNotNone(M.party_claim_issue({"name": "许乙", "role": "原告", "source_quote": "王甲、许乙："}, source))

    def test_acceptance_notice_you_sue_identifies_parties(self):
        source = "示例市人民法院\n受理通知书（小额诉讼）\n" + self.CASE + "\n示例甲公司：\n你诉示例乙公司买卖合同纠纷一案的起诉状已收到。"
        self.assertIsNone(M.party_claim_issue({"name": "示例甲公司", "role": "原告", "source_quote": "示例甲公司："}, source))
        self.assertIsNone(M.party_claim_issue({"name": "示例乙公司", "role": "被告", "source_quote": "你诉示例乙公司买卖合同纠纷一案"}, source))
        self.assertIsNotNone(M.party_claim_issue({"name": "示例乙公司", "role": "原告", "source_quote": "你诉示例乙公司买卖合同纠纷一案"}, source))

    def test_bare_dispute_order_and_unidentified_you_do_not_prove_roles(self):
        for source in ["受理通知书：甲公司与乙公司买卖合同纠纷一案已受理。",
                       "申请方甲公司与被申请方乙公司纠纷一案。",
                       "应诉通知书：甲公司与乙公司，双方权利义务告知。"]:
            self.assertIsNotNone(M.party_claim_issue({"name": "甲公司", "role": "原告", "source_quote": source}, source))
        source = "应诉通知书，代理人甲：本院已经受理乙公司诉你一案。"
        self.assertIsNotNone(M.party_claim_issue({"name": "甲", "role": "被告", "source_quote": "代理人甲："}, source))

    def test_semantic_role_does_not_waive_name_quote_requirement(self):
        source = "应诉通知书\n" + self.CASE + "\n王甲：本院已经受理甲公司诉你一案。"
        p = {"name": "王甲", "role": "被告", "source_quote": "本院已经受理甲公司诉你一案。"}
        self.assertIsNotNone(M.party_claim_issue(p, source))


if __name__ == "__main__":
    unittest.main()
