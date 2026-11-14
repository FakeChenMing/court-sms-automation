"""Independent workflow scenarios; no model, OCR, or Calendar side effects."""
import copy
import asyncio
import json
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import workflow_worker as worker
from calendar_content import BLOCK, source_attachments
from party_context import party_claim_issue


class FrozenTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 11, 14, 12, 0, tzinfo=worker.TZ)
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


class PolicyBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='court-policy-behavior-')
        self.addCleanup(self.temp.cleanup)
        self.clock = patch.object(worker, 'datetime', FrozenTime)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.root = Path(self.temp.name)
        self.ledger_patch = patch.multiple(worker.ledger, STATE_DIR=self.root, LEDGER=self.root / 'ledger.json')
        self.ledger_patch.start()
        self.addCleanup(self.ledger_patch.stop)
        self.state_patch = patch.object(worker, 'STATE', self.root)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        (self.root / 'ledger.json').write_text(json.dumps({'version': 1, 'documents': {'notice': {}}, 'events': {}, 'cases': {}}))
        self.pdf = self.root / '法院通知.pdf'
        self.pdf.write_bytes(b'%PDF-1.4\n%isolated fixture\n%%EOF\n')
        self.case = '（2026）测0113民初99999号'
        self.quote = '定于2026年12月4日09:30在第二法庭进行证据交换。'
        self.docs = [{'id': 'notice', 'source_path': str(self.pdf),
                      'source_sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest(), 'context_revision': 3,
                      'pages': [{'page': 1, 'text': self.quote + '原告甲公司，被告乙公司。'}]}]
        self.result = {
            'case_number': self.case,
            'documents': [{'id': 'notice', 'reviewed_pages': [1],
                           'date_inventory': [{'source_page': 1, 'source_quote': self.quote}],
                           'conclusion': '已逐页审查', 'unresolved': []}],
            'parties': [{'role': '原告', 'name': '甲公司', 'source_doc': 'notice',
                         'source_page': 1, 'source_quote': '原告甲公司'},
                        {'role': '被告', 'name': '乙公司', 'source_doc': 'notice',
                         'source_page': 1, 'source_quote': '被告乙公司'}],
            'events': [{'owner_doc': 'notice', 'category': '证据交换', 'subject': '本案证据交换',
                        'title': self.case + ' 证据交换',
                        'start': '2026-12-04T09:30:00+08:00',
                        'end': '2026-12-04T10:30:00+08:00', 'all_day': False,
                        'location': '第二法庭', 'source_page': 1, 'source_quote': self.quote,
                        'basis_type': 'document_explicit', 'action_summary': '携带证据原件。',
                        'explanation': '内部检索和审查详细说明' * 400}],
            'pending_checks': []}

    def check(self):
        return worker.validate(self.result, self.docs, ['notice'], self.case, [], [])

    def commit(self, existing=None):
        with patch.object(worker, 'calendar_read', return_value=existing or []), \
             patch.object(worker.ledger, 'record_review') as record, \
             patch.object(worker.ledger, 'apply_calendar') as apply, \
             patch.object(worker.ledger, 'record_receipt_group_review', create=True) as group, \
             patch.object(worker.ledger, 'apply_receipt_group_calendar', create=True):
            worker.commit(self.case, self.docs, ['notice'], self.result, [], [])
            self.group_decision=copy.deepcopy(group.call_args.args[1]) if group.called else None
            return copy.deepcopy(record.call_args.args[1]), apply.call_count

    def test_explicit_exchange_keeps_real_date_pdf_and_readable_notes(self):
        decision, writes = self.commit()
        event = decision['events'][0]
        self.assertEqual(event['start'], '2026-12-04T09:30:00+08:00')
        self.assertEqual(event['location'], '第二法庭')
        self.assertEqual(event['attachment_paths'], [str(self.pdf)])
        self.assertEqual(event['event_kind'], 'appointment')
        self.assertEqual(event['title'], self.case + '｜甲公司诉乙公司｜证据交换')
        self.assertIn('原告：甲公司', event['notes'])
        self.assertIn('被告：乙公司', event['notes'])
        self.assertIn('携带证据原件', event['notes'])
        self.assertNotIn('内部检索和审查详细说明', event['notes'])
        self.assertNotIn(str(self.root), event['notes'])
        self.assertLess(len(event['notes']), 500)
        self.assertEqual(writes, 1)

    def make_relative_unknown(self):
        quote = '请于收到本通知后十五日内提交证据。'
        self.docs[0]['pages'][0]['text'] = quote
        self.result['documents'][0]['date_inventory'] = [
            {'source_page': 1, 'source_quote': quote, 'status': 'anchor_unverified'}]
        self.result['parties'] = []
        self.result['events'] = []
        self.result['pending_checks'] = [{'owner_doc': 'notice', 'source_page': 1,
                                         'source_quote': quote, 'issue': '缺少有效送达日期'}]

    def test_relative_period_without_anchor_stays_in_pending_list(self):
        self.make_relative_unknown()
        decision, writes = self.commit()
        self.assertEqual(decision['events'], [])
        self.assertEqual(writes, 0)
        self.assertIn('缺少有效送达日期', decision['unresolved'])
        self.assertEqual(len(decision['pending_checks']), 1)

    def test_relative_period_cannot_be_written_on_processing_day(self):
        old = self.result['events'][0]
        self.make_relative_unknown()
        old.update(basis_type='internal_check', title=self.case + ' 待核',
                   start='2026-11-14T00:00:00+08:00', end='2026-11-15T00:00:00+08:00',
                   all_day=True, source_quote=self.docs[0]['pages'][0]['text'],
                   uncertainty='送达未知')
        self.result['events'] = [old]
        with self.assertRaises(AssertionError):
            self.check()

    def make_freeze_expiry(self):
        quote = '该银行存款冻结期限于2027年2月5日届满。'
        self.docs[0]['pages'][0]['text'] = quote
        self.docs[0]['receipt_binding'] = {'source_doc': 'notice', 'document_sha256': self.docs[0]['source_sha256'], 'kind': 'delivery_sms', 'source_rowid': 1, 'received_at': '2026-11-13T12:00:00+08:00', 'binding_kind': 'download_manifest', 'binding_source': 'fixture/.delivery_manifest.json'}
        self.result['parties'] = []
        self.result['documents'][0]['date_inventory'] = [{'source_page': 1, 'source_quote': quote}]
        self.result['events'][0].update(category='冻结届满', subject='涉案银行存款冻结届满',
                                       title=self.case + ' 冻结届满', source_quote=quote,
                                       start='2027-02-05T00:00:00+08:00',
                                       end='2027-02-06T00:00:00+08:00', all_day=True,
                                       action_summary='核对保全状态。')

    def test_explicit_freeze_expiry_keeps_date_and_seven_day_alarm(self):
        self.make_freeze_expiry()
        decision, _ = self.commit()
        event = decision['events'][0]
        self.assertEqual(event['start'][:10], '2027-02-05')
        self.assertTrue(event['all_day'])
        self.assertEqual(event['alarm_minutes'], -10080)
        self.assertEqual(event['attachment_paths'], [str(self.pdf)])
        # This test does not infer or fabricate a legal renewal deadline.
        self.assertEqual(len(decision['events']), 1)
        self.assertEqual(decision['receipt_group_keys'],[self.case+'|收到材料|2026-11-13'])
        self.assertTrue(self.group_decision['receipt_only'])
        self.assertNotIn('all_pages_reviewed',self.group_decision)
        received = self.group_decision['events'][0]
        self.assertEqual(received['event_kind'], 'receipt')
        self.assertEqual(received['start'][:10], '2026-11-13')
        self.assertEqual(received['due_dates'][0]['date'], '2027-02-05')
        self.assertNotIn('alarm_minutes', received)
        for node in decision['events']+[received]:
            self.assertIn('收到日期：2026-11-13', node['notes'])
            self.assertIn('到期日期：2027-02-05', node['notes'])

    def test_reused_receipt_uid_carries_fresh_title_and_notes_snapshot(self):
        self.make_freeze_expiry()
        self.commit()
        received = copy.deepcopy(self.group_decision['events'][0])
        uid = 'existing-receipt-uid'
        stale = {'uid': uid, 'title': received['title'],
                 'start_utc': '2026-11-12T16:00:00Z', 'notes': received['notes']}
        fresh = {**stale,
                 'title': '人工前缀·' + received['title'] + '·人工后缀',
                 'notes': '用户备注：核对原件。\n\n' + received['notes']
                          + '\n\n用户后补：已联系当事人。'}
        (self.root / 'calendar_title_sources.json').write_text(json.dumps({
            uid: {'title': received['title'], 'party_names': []}}))
        with patch.object(worker, 'calendar_read', return_value=[fresh]) as read_calendar, \
             patch.object(worker.ledger, 'record_review'), \
             patch.object(worker.ledger, 'apply_calendar'), \
             patch.object(worker.ledger, 'record_receipt_group_review', create=True) as record_group, \
             patch.object(worker.ledger, 'apply_receipt_group_calendar', create=True):
            worker.commit(self.case, self.docs, ['notice'], self.result, [], [stale])
        read_calendar.assert_called_once_with(self.case)
        record_group.assert_called_once()
        key, decision = record_group.call_args.args
        event = decision['events'][0]
        self.assertEqual(key, received['event_key'])
        self.assertEqual(event['existing_uid'], uid)
        self.assertEqual(event['expected_previous_title'], fresh['title'])
        self.assertEqual(event['expected_previous_notes'], fresh['notes'])
        self.assertNotEqual(event['expected_previous_notes'], stale['notes'])
        self.assertEqual(event['title'], fresh['title'])
        self.assertEqual(event['notes'].count('用户备注：核对原件。'), 1)
        self.assertEqual(event['notes'].count('用户后补：已联系当事人。'), 1)
        self.assertTrue(decision['receipt_only'])
        self.assertNotIn('all_pages_reviewed', decision)

    def test_known_expiry_without_exact_receipt_stays_pending(self):
        self.make_freeze_expiry()
        del self.docs[0]['receipt_binding']
        with self.assertRaisesRegex(ValueError, '收到记录'):
            self.check()
        self.assertEqual(self.result['events'][0]['start'], '2027-02-05T00:00:00+08:00')

    def test_wrong_model_kind_cannot_suppress_deadline_alarm(self):
        self.make_freeze_expiry()
        self.result['events'][0]['event_kind'] = 'appointment'
        try:
            decision, _ = self.commit()
        except (AssertionError, ValueError):
            return  # A conflicting classification may also be rejected.
        self.assertEqual(decision['events'][0].get('alarm_minutes'), -10080)

    def test_greeting_recipient_rejected_by_source_guard(self):
        text = '【某法院】律师甲你好，请查收' + self.case + '案件中的送达文书。'
        party = {'name': '律师甲', 'role': '当事人（角色未明）',
                 'source_message_id': 'sms1', 'source_quote': text}
        self.assertIsNotNone(party_claim_issue(party, text))

    def test_greeting_recipient_rejected_at_worker_boundary(self):
        text = '【某法院】律师甲你好，请查收' + self.case + '案件中的送达文书。'
        self.docs[0]['same_case_messages'] = [{'id': 'sms1', 'received_at': '2026-11-14', 'text': text}]
        self.result['parties'] = [{'name': '律师甲', 'role': '当事人（角色未明）',
                                   'source_message_id': 'sms1', 'source_quote': text}]
        with self.assertRaises(AssertionError):
            self.check()

    def test_retry_recovers_uid_and_preserves_manual_prefix(self):
        key = self.case + '|证据交换|本案证据交换'
        existing = [{'uid': 'saved-uid', 'start_utc': '2026-12-04T01:30:00Z',
                     'notes': '用户备注：请带合同原件。\n\n' + BLOCK + '\n法院材料事项ID：' + key}]
        first, _ = self.commit(existing)
        e = first['events'][0]
        self.assertEqual(e['existing_uid'], 'saved-uid')
        existing[0]['notes'] = e['notes']
        second, _ = self.commit(existing)
        self.assertEqual(second['events'][0]['existing_uid'], 'saved-uid')
        self.assertEqual(second['events'][0]['notes'].count('请带合同原件'), 1)

    def test_manual_note_appended_after_managed_block_survives_retry(self):
        key = self.case + '|证据交换|本案证据交换'
        existing = [{'uid': 'saved-uid', 'start_utc': '2026-12-04T01:30:00Z',
                     'notes': BLOCK + '\n法院材料事项ID：' + key + '\n用户备注：请带合同原件。'}]
        decision, _ = self.commit(existing)
        self.assertIn('用户备注：请带合同原件。', decision['events'][0]['notes'])

    def test_verified_title_parties_cannot_disappear_on_later_review(self):
        title = self.case + '｜甲公司诉乙公司｜证据交换'
        existing = [{'uid': 'title-uid', 'title': title,
                     'start_utc': '2026-12-04T01:30:00Z', 'notes': ''}]
        self.result['events'][0]['existing_uid'] = 'title-uid'
        self.result['parties'] = self.result['parties'][:1]
        (self.root / 'calendar_title_sources.json').write_text(json.dumps({
            'title-uid': {'title': title, 'party_names': ['甲公司', '乙公司']}}))
        with patch.object(worker, 'STATE', self.root), self.assertRaises(AssertionError):
            self.commit(existing)

    def test_legacy_title_can_gain_verified_party_using_saved_title_audit(self):
        title = self.case + '｜甲公司（另一方未确认）｜证据交换'
        existing = [{'uid': 'legacy-uid', 'title': title,
                     'start_utc': '2026-12-04T01:30:00Z', 'notes': ''}]
        self.result['events'][0]['existing_uid'] = 'legacy-uid'
        (self.root / 'calendar_title_sources.json').write_text(json.dumps({
            'legacy-uid': {'title': title, 'party_names': ['甲公司']}}))
        with patch.object(worker, 'STATE', self.root):
            decision, _ = self.commit(existing)
        self.assertEqual(decision['events'][0]['title'], self.case + '｜甲公司诉乙公司｜证据交换')

    def test_verified_receipt_parties_survive_without_manual_title_audit(self):
        quote = '收到本举证通知后十五日内提交证据。'
        self.docs[0].update(source_sha256='a' * 64,
                            pages=[{'page': 1, 'text': quote + '原告甲公司，被告乙公司。'}],
                            receipt_binding={'source_doc': 'notice', 'document_sha256': 'a' * 64,
                                             'kind': 'delivery_sms', 'source_rowid': 1,
                                             'received_at': '2026-11-13T12:00:00+08:00',
                                             'binding_kind': 'download_manifest',
                                             'binding_source': 'fixture/.delivery_manifest.json'})
        key = self.case + '|收到材料|' + 'a' * 64
        title = self.case + '｜甲公司诉乙公司｜收到举证通知书'
        self.result['events'] = []
        self.result['documents'][0]['date_inventory'] = [{'source_page': 1, 'source_quote': quote}]
        self.result['receipt_materials'] = [{'owner_doc': 'notice', 'source_page': 1,
                                             'source_quote': quote, 'document_label': '举证通知书'}]
        self.result['parties'] = self.result['parties'][:1]
        existing = [{'uid': 'verified-receipt-uid', 'title': title,
                     'start_utc': '2026-11-12T16:00:00Z',
                     'notes': BLOCK + '\n法院材料事项ID：' + key}]
        state = {'version': 1, 'cases': {}, 'documents': {'notice': {
            'receipt_decision': {'events': [{'event_key': key,
                                            'parties': {'原告': ['甲公司'], '被告': ['乙公司']}}]}}},
            'events': {key: {'uid': 'verified-receipt-uid', 'title': title, 'verified': True}}}
        worker.ledger.LEDGER.write_text(json.dumps(state, ensure_ascii=False))
        before = worker.ledger.LEDGER.read_bytes()
        self.assertFalse((self.root / 'calendar_title_sources.json').exists())
        with patch.object(worker, 'calendar_read', return_value=existing), \
             patch.object(worker.ledger, 'record_review') as record, \
             patch.object(worker.ledger, 'apply_calendar') as apply:
            with self.assertRaisesRegex(AssertionError, '已核实的标题主体'):
                worker.commit(self.case, self.docs, ['notice'], self.result, [], [])
            record.assert_not_called()
            apply.assert_not_called()
        self.assertEqual(worker.ledger.LEDGER.read_bytes(), before)

    def test_cross_document_date_keeps_both_source_pdfs(self):
        second = self.root / '调查排期.pdf'
        second.write_bytes(b'%PDF-1.4\n%isolated fixture\n%%EOF\n')
        documents = {d['id']: d for d in self.docs}
        documents['schedule'] = {'source_path': str(second)}
        event = {'date_reference': {'source_doc': 'schedule', 'source_page': 1}}
        self.assertEqual(source_attachments(event, 'notice', documents), [str(self.pdf), str(second)])

    def test_verified_activity_correction_cannot_regress_to_old_hearing(self):
        quote='传唤事由询问，定于2026年12月4日09:30在第二法庭进行。'
        self.docs[0]['pages'][0]['text']=quote+'原告甲公司，被告乙公司。'
        self.result['documents'][0]['date_inventory']=[{'source_page':1,'source_quote':quote}]
        self.result['events'][0].update(category='开庭',subject='开庭',action_label='开庭',source_quote=quote,existing_uid='corrected-uid')
        title=self.case+'｜甲公司诉乙公司｜询问'
        evidence={'source_path':str(self.pdf),'source_sha256':self.docs[0]['source_sha256'],'source_page':1,'source_quote':quote,'correct_activity':'询问'}
        audit={'case_number':self.case,'title':title,'party_names':['甲公司','乙公司'],'action_label':'询问','action_evidence':evidence}
        (self.root/'calendar_title_sources.json').write_text(json.dumps({'corrected-uid':audit}))
        existing=[{'uid':'corrected-uid','title':title,'start_utc':'2026-12-04T01:30:00Z','notes':''}]
        decision,_=self.commit(existing)
        self.assertEqual(decision['events'][0]['title'],title)
        self.assertEqual(decision['events'][0]['activity_source']['source_quote'],quote)
        evidence['source_sha256']='stale'
        (self.root/'calendar_title_sources.json').write_text(json.dumps({'corrected-uid':audit}))
        with self.assertRaisesRegex(AssertionError,'SHA'):self.commit(existing)

    def test_ready_group_recovers_even_without_model_or_pending_documents(self):
        state={'documents':{},'receipt_groups':{'ready':{'case_number':self.case,'receipt_review_status':'ready'},'done':{'case_number':self.case,'receipt_review_status':'complete'}}}
        worker.ledger.LEDGER.write_text(json.dumps(state))
        with patch.object(worker.ledger,'apply_receipt_group_calendar',create=True) as apply:
            asyncio.run(worker.recover_receipt_groups(state))
            apply.assert_called_once_with('ready')
        with patch.object(worker.ledger,'apply_receipt_group_calendar',create=True) as apply:
            asyncio.run(worker.recover_receipt_groups(state,preview=True))
            apply.assert_not_called()

    def test_missing_attachment_or_unverified_alarm_prevents_completion(self):
        spec = importlib.util.spec_from_file_location('policy_test_ledger', SCRIPTS / 'runtime/court_document_review.py')
        ledger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger)
        event = {'basis_type': 'document_explicit', 'event_kind': 'deadline',
                 'alarm_minutes': -10080, 'attachment_paths': [str(self.pdf)]}
        event['attachment_manifest'] = ledger.calendar_event_requirements(event)
        empty = {'extras_verified': True, 'metadata_preserved': True, 'attachments': [], 'alarms': []}
        with self.assertRaises(AssertionError):
            ledger.verify_extras_receipt(event, empty)
        receipt = copy.deepcopy(empty)
        receipt['attachments'] = [{**event['attachment_manifest'][0], 'content_verified': True}]
        with self.assertRaises(AssertionError):
            ledger.verify_extras_receipt(event, receipt)
        receipt['alarms'] = [{'relative_minutes': -10080, 'absolute_timestamp': None}]
        ledger.verify_extras_receipt(event, receipt)


if __name__ == '__main__':
    unittest.main()
