"""Exact transport bindings and pair composition; all input is temporary fiction."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from receipt_binding import verified_binding, attach_binding, message_delivery_keys, sms_timestamp
from dual_nodes import add_receipt_nodes, receipt_key, combine_receipt_events, retained_receipt_events
from calendar_content import concise_notes, calendar_title, source_attachments

def apple_ns(value):
    return round((datetime.fromisoformat(value) - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1_000_000_000)


class ExactReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / '举证通知书.pdf'
        self.pdf.write_bytes(b'%PDF-1.4\nfixture\n%%EOF')
        self.sha = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.url = 'https://sdpt.court.gov.cn/#/pagesAjkj/app/wssd/index?qdbh=channel&sdbh=delivery'
        self.delivery_key = hashlib.sha256(b'channel|delivery').hexdigest()[:20]
        self.manifest = self.root / '.delivery_manifest.json'
        self.manifest.write_text(json.dumps({'delivery_key': self.delivery_key, 'documents': [{'path': str(self.pdf), 'sha256': self.sha}]}))
        self.doc = {'id': 'doc1', 'case_number': '（2026）测01民初1号', 'sha256': self.sha, 'paths': [str(self.pdf)], 'deliveries': []}
        self.state = {'message_jobs': {'10': {'received_at': '2026-01-03T12:00:00+08:00', 'message': {'rowid': 10, 'text': self.url, 'date': apple_ns('2026-01-03T12:00:00+08:00')}}}}

    def test_exact_path_hash_and_delivery_identifier_are_required(self):
        result = verified_binding(self.doc, self.state)
        self.assertEqual(result['source_rowid'], 10)
        self.assertEqual(result['document_sha256'], self.sha)
        self.assertEqual(result['binding_kind'], 'download_manifest')
        self.state['message_jobs']['10']['message']['text'] = '同案短信，没有本件送达标识'
        self.assertIsNone(verified_binding(self.doc, self.state))

    def test_same_filename_wrong_path_or_pdf_content_does_not_match(self):
        wrong = json.loads(self.manifest.read_text());wrong['documents'][0]['path'] = str(self.root / 'other' / self.pdf.name)
        self.manifest.write_text(json.dumps(wrong));self.assertIsNone(verified_binding(self.doc, self.state))
        wrong['documents'][0]['path'] = str(self.pdf);self.manifest.write_text(json.dumps(wrong));self.pdf.write_bytes(b'%PDF-1.4\nmodified')
        self.assertIsNone(verified_binding(self.doc, self.state))

    def test_earliest_exact_sms_wins_without_old_week_association(self):
        self.doc['deliveries'] = [{'source_rowid': 3, 'received_at': '2026-01-01T09:00:00+08:00'}]
        self.state['message_jobs']['11'] = {'received_at': '2026-01-02T18:00:00+08:00', 'message': {'rowid': 11, 'text': self.url, 'date': apple_ns('2026-01-02T18:00:00+08:00')}}
        result = attach_binding(self.doc, self.state)
        self.assertEqual(result['source_rowid'], 11)
        self.assertTrue(self.doc['receipt_binding_verified'])
        self.assertEqual(len(self.doc['deliveries']), 2)

    def test_before_2026_future_or_fake_court_domain_cannot_bind(self):
        for received in ('2025-12-31T23:59:59+08:00', '2099-01-01T12:00:00+08:00'):
            self.state['message_jobs']['10']['received_at'] = received
            self.state['message_jobs']['10']['message']['date'] = apple_ns(received)
            self.assertIsNone(verified_binding(self.doc, self.state))
        self.assertEqual(message_delivery_keys(self.url.replace('court.gov.cn', 'court.gov.cn.example.com')), set())

    def test_failed_reverification_invalidates_previously_verified_binding(self):
        attach_binding(self.doc, self.state)
        self.manifest.unlink()
        self.assertIsNone(attach_binding(self.doc, self.state))
        self.assertIsNot(self.doc.get('receipt_binding_verified'), True)
        self.assertIsNone(self.doc.get('receipt_binding'))

    def test_sms_raw_date_must_confirm_cached_received_time(self):
        self.state['message_jobs']['10']['received_at'] = '2026-01-04T12:00:00+08:00'
        self.assertIsNone(verified_binding(self.doc, self.state))
        self.assertIn('sms_timestamp_mismatch', [e['kind'] for e in self.doc['receipt_binding_errors']])
        self.state['message_jobs']['10']['message'].pop('date')
        self.assertIsNone(verified_binding(self.doc, self.state))
        self.assertIn('invalid_sms_timestamp', [e['kind'] for e in self.doc['receipt_binding_errors']])

    def test_corrupt_manifest_and_timestamp_do_not_hide_other_valid_bindings(self):
        bad = self.root / 'bad';bad.mkdir();bad_pdf = bad / self.pdf.name;bad_pdf.write_bytes(self.pdf.read_bytes());(bad / self.manifest.name).write_text('{malformed')
        self.doc['paths'].insert(0, str(bad_pdf))
        self.state['message_jobs']['9'] = {'received_at': 'malformed', 'message': {'rowid': 9, 'text': self.url, 'date': 'NaN'}}
        self.assertEqual(verified_binding(self.doc, self.state)['source_rowid'], 10)
        self.assertEqual({e['kind'] for e in self.doc['receipt_binding_errors']}, {'invalid_download_manifest', 'invalid_sms_timestamp'})
        self.assertNotIn('https', json.dumps(self.doc['receipt_binding_errors']))

    def test_apple_nanosecond_and_legacy_second_epochs_agree(self):
        expected = '2026-01-03T12:00:00+08:00';raw = apple_ns(expected)
        self.assertEqual(sms_timestamp(raw).isoformat(), expected)
        self.assertEqual(sms_timestamp(raw // 1_000_000_000).isoformat(), expected)
        for invalid in (None, True, 0, 'NaN', 'Infinity', 'not-a-date'):
            with self.assertRaises(ValueError):sms_timestamp(invalid)


class PairCompositionTests(unittest.TestCase):
    def setUp(self):
        self.case = '（2026）测01民初1号'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / '举证通知书.pdf'
        self.pdf.write_bytes(b'%PDF fixture evidence notice')
        self.sha = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.binding = {'source_doc': 'doc1', 'document_sha256': self.sha, 'kind': 'delivery_sms', 'source_rowid': 10, 'received_at': '2026-01-03T12:00:00+08:00', 'binding_kind': 'download_manifest', 'binding_source': 'fixture/.delivery_manifest.json'}
        self.doc = {'id': 'doc1', 'source_sha256': self.sha, 'source_path': str(self.pdf), 'receipt_binding': self.binding}
        self.deadline = {'owner_doc': 'doc1', 'category': '举证期限', 'subject': '提交证据', 'source_page': 1, 'source_quote': '请于2026年12月1日前举证', 'basis_type': 'document_explicit', 'start': '2026-12-01T00:00:00+08:00', 'end': '2026-12-02T00:00:00+08:00', 'all_day': True, 'event_key': 'old-stable-deadline-key', 'existing_uid': 'old-uid'}
        self.result = {'case_number': self.case, 'events': [self.deadline], 'pending_checks': []}

    def test_pair_keeps_original_deadline_uid_key_and_date(self):
        add_receipt_nodes(self.result, [self.doc])
        deadline, received = self.result['events']
        self.assertEqual((deadline['event_key'], deadline['existing_uid'], deadline['due_date']), ('old-stable-deadline-key', 'old-uid', '2026-12-01'))
        self.assertEqual(deadline['received_date'], '2026-01-03')
        self.assertEqual(received['event_key'], receipt_key(self.case, self.doc))
        self.assertEqual(received['due_dates'], [{'subject': '提交证据', 'date': '2026-12-01', 'event_key': 'old-stable-deadline-key'}])
        self.assertEqual(received['start'], '2026-01-03T00:00:00+08:00')
        self.assertNotIn('alarm_minutes', received)

    def test_repeated_composition_deduplicates_receipt_and_stable_key_ignores_title(self):
        add_receipt_nodes(self.result, [self.doc]);key = self.result['events'][-1]['event_key']
        self.result['receipt_materials'] = [{'owner_doc': 'doc1', 'document_label': '模型改写的举证通知', 'source_page': 1, 'source_quote': self.deadline['source_quote']}]
        add_receipt_nodes(self.result, [self.doc]);self.assertEqual(len(self.result['events']), 2);self.assertEqual(self.result['events'][-1]['event_key'], key)

    def test_unknown_due_date_is_explicitly_pending_without_fabricated_date(self):
        self.result['events'] = []
        self.result['pending_checks'] = [{'owner_doc': 'doc1', 'source_page': 1, 'source_quote': '收到后十五日内举证', 'deadline_related': True, 'issue': '有效送达日期待核'}]
        add_receipt_nodes(self.result, [self.doc]);received = self.result['events'][0]
        self.assertEqual(received['due_dates'], []);self.assertEqual(received['pending_deadlines'], ['有效送达日期待核'])
        received['case_number'] = self.case
        notes = concise_notes(received, [], 'doc1', {'doc1': self.doc})
        self.assertIn('收到日期：2026-01-03', notes);self.assertIn('到期日期：待核', notes)
        detailed = '法宝legal-3、元典legal-4已核验。' + '有效送达及具体期限仍待核实。' * 30
        received['pending_deadlines'] = [detailed]
        concise = concise_notes(received, [], 'doc1', {'doc1': self.doc})
        self.assertIn('到期日期：待核（核实起算日期及具体期限后补录）', concise)
        self.assertNotIn('legal-3', concise)
        self.assertEqual(received['pending_deadlines'], [detailed])

    def test_pending_uncertainty_alias_is_accepted_before_pair_building(self):
        self.result['events'] = []
        self.result['pending_checks'] = [{'owner_doc': 'doc1', 'source_page': 1, 'source_quote': '收到后十五日内举证', 'deadline_related': True, 'uncertainty': '有效送达日期待核'}]
        add_receipt_nodes(self.result, [self.doc])
        self.assertEqual(self.result['events'][0]['pending_deadlines'], ['有效送达日期待核'])

    def test_missing_receipt_binding_does_not_guess_date(self):
        del self.doc['receipt_binding']
        with self.assertRaisesRegex(ValueError, '收到记录'):add_receipt_nodes(self.result, [self.doc])
        self.assertEqual(self.deadline['start'], '2026-12-01T00:00:00+08:00')

    def test_same_parties_pdf_and_both_dates_are_present_in_both_nodes(self):
        parties = [{'role': '原告', 'name': '甲公司'}, {'role': '被告', 'name': '乙公司'}]
        add_receipt_nodes(self.result, [self.doc])
        for event in self.result['events']:
            event['case_number'] = self.case
            notes = concise_notes(event, parties, 'doc1', {'doc1': self.doc})
            self.assertIn('甲公司诉乙公司', calendar_title(event, parties))
            self.assertIn('收到日期：2026-01-03', notes);self.assertIn('到期日期：2026-12-01', notes)
            self.assertEqual(source_attachments(event, 'doc1', {'doc1': self.doc}), [self.doc['source_path']])
            if event['event_kind'] == 'deadline':self.assertIn('提前7日', notes)

    def second_document(self, received='2026-01-03T18:00:00+08:00'):
        pdf = Path(self.temp.name) / '缴费通知书.pdf'
        pdf.write_bytes(b'%PDF fixture fee notice')
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        binding = {**self.binding, 'source_doc':'doc2', 'document_sha256':sha, 'received_at':received, 'source_rowid':11}
        return {'id':'doc2','source_path':str(pdf),'source_sha256':sha,'receipt_binding':binding}

    def test_same_day_materials_one_receipt_distinct_deadlines_and_both_pdfs(self):
        doc2 = self.second_document()
        self.result['events'].append({**self.deadline,'owner_doc':'doc2','category':'缴费期限','subject':'缴费','event_key':'fee-key','start':'2026-12-05T00:00:00+08:00','end':'2026-12-06T00:00:00+08:00'})
        add_receipt_nodes(self.result,[self.doc,doc2])
        self.assertEqual(len(self.result['events']),3)
        receipt = self.result['events'][-1]
        self.assertEqual(receipt['event_key'],self.case+'|收到材料|2026-01-03')
        self.assertEqual(len(receipt['receipt_documents']),2)
        self.assertEqual({x['date'] for x in receipt['due_dates']},{'2026-12-01','2026-12-05'})
        self.assertEqual(set(source_attachments(receipt,'doc1',{'doc1':self.doc,'doc2':doc2})),{self.doc['source_path'],doc2['source_path']})
        for event in self.result['events'][:2]:self.assertEqual(event['paired_receipt_key'],receipt['event_key'])

    def test_later_batch_retains_other_verified_members_and_dates(self):
        doc2=self.second_document()
        first=copy.deepcopy(self.result);add_receipt_nodes(first,[self.doc]);old=first['events'][-1]
        state={'events':{old['event_key']:{'verified':True}},'receipt_groups':{old['event_key']:{'receipt_decision':{'events':[old]}}}}
        later={'case_number':self.case,'events':[],'receipt_materials':[{'owner_doc':'doc2','document_label':'缴费通知书','source_page':1,'source_quote':'收到后七日内缴费'}],'pending_checks':[{'owner_doc':'doc2','source_page':1,'source_quote':'收到后七日内缴费','deadline_related':True,'issue':'送达日待核'}]}
        add_receipt_nodes(later,[doc2])
        retained=retained_receipt_events(self.case,later['events'],state)
        grouped=combine_receipt_events(later['events']+retained)
        self.assertEqual(len(grouped),1)
        self.assertEqual({m['id'] for m in grouped[0]['receipt_documents']},{'doc1','doc2'})
        self.assertEqual(grouped[0]['due_dates'][0]['date'],'2026-12-01')
        self.assertEqual(grouped[0]['pending_deadlines'],['送达日待核'])
        self.assertEqual(len(grouped[0]['attachment_paths']),2)
        member=next(m for m in grouped[0]['receipt_documents'] if m['id']=='doc1')
        self.assertEqual(member['review_basis'],'verified_receipt')
        self.assertEqual(member['receipt_event_key'],old['event_key'])

    def test_repeated_same_sha_does_not_create_later_receipt(self):
        add_receipt_nodes(self.result,[self.doc]);first=self.result['events'][-1]
        repeat=copy.deepcopy(first);repeat['receipt_documents'][0]['binding']['received_at']='2026-01-05T10:00:00+08:00'
        repeat['received_date']='2026-01-05';repeat['event_key']=self.case+'|收到材料|2026-01-05'
        grouped=combine_receipt_events([repeat,first])
        self.assertEqual(len(grouped),1);self.assertEqual(grouped[0]['event_key'],first['event_key'])
        self.assertEqual(len(grouped[0]['receipt_documents']),1)

    def test_different_days_or_complete_case_numbers_do_not_merge(self):
        add_receipt_nodes(self.result,[self.doc]);first=self.result['events'][-1]
        second=copy.deepcopy(first);second['case_number']='（2026）测02民初1号'
        self.assertEqual(len(combine_receipt_events([first,second])),2)
        doc2=self.second_document('2026-01-04T10:00:00+08:00')
        result={'case_number':self.case,'events':[],'receipt_materials':[{'owner_doc':'doc2','source_page':1,'source_quote':'收到后七日内缴费'}]}
        add_receipt_nodes(result,[doc2]);self.assertEqual(len(combine_receipt_events([first]+result['events'])),2)

    def test_group_attachment_hash_mismatch_blocks_mutation(self):
        add_receipt_nodes(self.result,[self.doc]);self.pdf.write_bytes(b'%PDF changed after review')
        with self.assertRaisesRegex(ValueError,'SHA'):
            source_attachments(self.result['events'][-1],'doc1',{'doc1':self.doc})


if __name__ == '__main__':unittest.main()
