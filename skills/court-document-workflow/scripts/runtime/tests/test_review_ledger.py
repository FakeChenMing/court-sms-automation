import sys,pathlib,unittest,tempfile,json,datetime
from unittest import mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import court_document_review as m
class LedgerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
  self.patch=mock.patch.multiple(m,STATE_DIR=self.root,LEDGER=self.root/'state.json');self.patch.start()
  self.file=self.root/'材料.pdf';self.file.write_bytes(b'original');self.case='（2026）测0114民初90001号'
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def enqueue(self):return m.enqueue_documents(self.case,[self.file],'2026-11-13T00:00:00',1)[0]
 def ready(self):
  id=self.enqueue();t=self.root/'text.md';t.write_text('complete OCR');skill=self.root/'SKILL.md';skill.write_text('test skill')
  m.register_ocr(id,str(t),'test-engine',str(skill),1,m.digest(self.file));return id
 def decision(self):return {'source_sha256':m.digest(self.file),'all_pages_reviewed':True,'date_inventory':[],'conclusion':'无待办理日期','events':[]}
 def test_normalize_full_case_only(self):self.assertEqual(m.canonical_case('(2026) 测0114民初90001号'),self.case);self.assertEqual(m.canonical_case('90001'),'')
 def test_repeat_delivery_dedup(self):
  a=self.enqueue();b=m.enqueue_documents(self.case,[self.file],'2026-11-14T00:00:00',2)[0];self.assertEqual(a,b)
  self.assertEqual(len(json.loads(m.LEDGER.read_text())['documents'][a]['deliveries']),2)
 def test_changed_content_new_job(self):
  a=self.enqueue();self.file.write_bytes(b'changed');self.assertNotEqual(a,self.enqueue())
 def test_different_case_not_merged(self):
  a=self.enqueue();b=m.enqueue_documents('（2026）测0113民初90001号',[self.file],'2026-11-13',2)[0];self.assertNotEqual(a,b)
 def test_old_sms_rejected(self):
  with self.assertRaises(ValueError):m.enqueue_documents(self.case,[self.file],'2025-12-31',1)
 def test_corrupt_ledger_not_reset(self):
  m.LEDGER.write_text('{broken')
  with self.assertRaises(json.JSONDecodeError):self.enqueue()
  self.assertEqual(m.LEDGER.read_text(),'{broken')
 def test_review_requires_ocr(self):
  id=self.enqueue()
  with self.assertRaises(AssertionError):m.record_review(id,self.decision())
 def test_ocr_hash_mismatch_rejected(self):
  id=self.enqueue();t=self.root/'t.md';t.write_text('x')
  with self.assertRaises(AssertionError):m.register_ocr(id,str(t),'test',str(t),1,'wrong')
 def test_all_pages_required(self):
  id=self.ready();d=self.decision();d['all_pages_reviewed']=False
  with self.assertRaises(AssertionError):m.record_review(id,d)
 def test_no_date_can_complete(self):
  id=self.ready();m.record_review(id,self.decision());self.assertEqual(json.loads(m.LEDGER.read_text())['documents'][id]['review_status'],'complete')
 def test_unresolved_not_complete(self):
  id=self.ready();d=self.decision();d['unresolved']=['签收日缺失'];m.record_review(id,d);self.assertEqual(json.loads(m.LEDGER.read_text())['documents'][id]['review_status'],'attention')
 def test_changed_ocr_blocks_review(self):
  id=self.ready();(self.root/'text.md').write_text('changed')
  with self.assertRaises(AssertionError):m.record_review(id,self.decision())
 def test_new_same_case_document_requests_refresh(self):
  id=self.ready();m.record_review(id,self.decision());self.file.write_bytes(b'newnames');self.enqueue();self.assertTrue(json.loads(m.LEDGER.read_text())['documents'][id]['context_refresh_needed'])
 def test_event_requires_verified_basis(self):
  id=self.ready();d=self.decision();d['events']=[{'case_number':self.case,'category':'异议'}]
  with self.assertRaises(AssertionError):m.record_review(id,d)
