"""No Calendar access: exercise the write/verify boundary with simulated stores."""
import importlib.util,json,tempfile,unittest,sqlite3,copy,subprocess
from pathlib import Path
from unittest import mock
from datetime import datetime,timedelta,timezone

RUNTIME=Path(__file__).resolve().parents[1]/'scripts/runtime'
spec=importlib.util.spec_from_file_location('review_io_test_module',RUNTIME/'court_document_review.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class CalendarIOTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.helper=self.root/'helper';self.helper.write_text('simulated')
  self.patch=mock.patch.multiple(m,STATE_DIR=self.root,LEDGER=self.root/'ledger.json',ATTACHMENT_HELPER=self.helper,CALENDAR_DB=self.root/'calendar.sqlite');self.patch.start()
  # Calendar boundary scenarios inject the transport; separate tests exercise LaunchServices itself.
  self.transport_patch=mock.patch.object(m,'run_attachment_helper',side_effect=lambda mode,path,timeout=180:subprocess.run([str(m.ATTACHMENT_HELPER),mode,str(path)],capture_output=True,text=True,timeout=timeout));self.transport_patch.start()
  self.pdf=self.root/'日期.pdf';self.pdf.write_bytes(b'%PDF-1.4\nsource date\n%%EOF')
  self.text=self.root/'ocr.md';self.text.write_text('2035年1月2日举证到期')
  self.case='（2035）测0102民初100号';self.key=self.case+'|举证|提交证据'
  self.start=(datetime.now(timezone(timedelta(hours=8)))+timedelta(days=30)).replace(hour=0,minute=0,second=0,microsecond=0)
  with mock.patch.object(m,'wake_independent_worker'):
   self.docid=m.enqueue_documents(self.case,[self.pdf],'2026-01-02T12:00:00+08:00',1)[0]
  m.register_ocr(self.docid,str(self.text),'test',str(self.text),1,m.digest(self.pdf))
  self.receipt_key=self.case+'|收到材料|2026-01-02'
  state=self.state();state['documents'][self.docid].update(receipt_binding_verified=True,receipt_binding=self.evidence());m.atomic(m.LEDGER,state)
 def tearDown(self):self.transport_patch.stop();self.patch.stop();self.temp.cleanup()
 def event(self):
  return {'case_number':self.case,'event_key':self.key,'title':self.case+' 举证到期','start':self.start.isoformat(),'end':(self.start+timedelta(days=1)).isoformat(),'all_day':True,'location':'','notes':'原告：甲\n法院材料事项ID：'+self.key,'basis_type':'document_explicit','source_quote':'2035年1月2日举证到期','source_page':1,'category':'举证','parties_status':'unknown','attachment_paths':[str(self.pdf)],'is_deadline':True,'alarm_minutes':-10080,'event_kind':'deadline','received_date':'2026-01-02','due_date':self.start.date().isoformat(),'paired_receipt_key':self.receipt_key,'received_evidence':self.evidence()}
 def evidence(self):
  return {'source_rowid':1,'received_at':'2026-01-02T12:00:00+08:00','kind':'delivery_sms','source_doc':self.docid,'document_sha256':m.digest(self.pdf),'binding_kind':'download_manifest','binding_source':'fixture/download_manifest.json#rowid=1'}
 def receipt_event(self):
  event=self.event();event.update(event_kind='receipt',basis_type='document_received',event_key=self.receipt_key,title=self.case+' 收到举证通知书',start='2026-01-02T00:00:00+08:00',end='2026-01-03T00:00:00+08:00',notes='当事人：待核\n收到日期：2026-01-02\n法院材料事项ID：'+self.receipt_key,is_deadline=False,due_dates=[{'subject':'举证','date':self.start.date().isoformat(),'event_key':self.key}],pending_deadlines=[])
  event['receipt_documents']=[{'id':self.docid,'sha256':m.digest(self.pdf),'source_path':str(self.pdf),'binding':event['received_evidence'],'source_page':1,'source_quote':event['source_quote'],'material_type':'举证通知书','review_basis':'complete_document_review'}]
  for key in ('alarm_minutes','due_date','paired_receipt_key'):event.pop(key,None)
  return event
 def decision(self,event=None):
  return {'calendar_policy_version':4,'source_sha256':m.digest(self.pdf),'all_pages_reviewed':True,'date_inventory':[],'conclusion':'日期明确','events':[event or self.event()]}
 def receipt_decision(self):
  event=self.receipt_event();event.update(due_dates=[],pending_deadlines=['期限尚待完整审查'])
  record=self.root/'visual-review.md';record.write_text('测试原图审查：第 1 页举证通知书，文书哈希 '+m.digest(self.pdf))
  visual={'source_sha256':m.digest(self.pdf),'page':event['source_page'],'source_quote':event['source_quote'],'material_type':'举证通知书','method':'visual','verified_at':'2026-01-03T12:00:00+08:00','review_record':str(record)}
  event['receipt_documents'][0].update(review_basis='material_page_review',material_page_review=visual)
  return {'calendar_policy_version':4,'receipt_only':True,'source_sha256':m.digest(self.pdf),'events':[event],'material_page_review':visual}
 def group_decision(self):
  decision=self.receipt_decision();event=decision['events'][0]
  second=self.root/'缴费通知.pdf';second.write_bytes(b'%PDF-1.4\nsecond source\n%%EOF')
  with mock.patch.object(m,'wake_independent_worker'):docid=m.enqueue_documents(self.case,[second],'2026-01-02T13:00:00+08:00',2)[0]
  binding={**self.evidence(),'source_doc':docid,'document_sha256':m.digest(second),'source_rowid':2,'received_at':'2026-01-02T13:00:00+08:00'}
  state=self.state();state['documents'][docid].update(receipt_binding_verified=True,receipt_binding=binding);m.atomic(m.LEDGER,state)
  record=self.root/'second-visual.md';record.write_text('第1页实际核对缴费通知书 '+m.digest(second))
  member={'id':docid,'sha256':m.digest(second),'source_path':str(second),'binding':binding,'source_page':1,'source_quote':'请收到后缴费','material_type':'缴费通知书','review_basis':'material_page_review','material_page_review':{'source_sha256':m.digest(second),'page':1,'source_quote':'请收到后缴费','material_type':'缴费通知书','method':'visual','verified_at':'2026-01-03T12:00:00+08:00','review_record':str(record)}}
  event['receipt_documents'].append(member);event['attachment_paths'].append(str(second))
  return decision
 def state(self):return json.loads(m.LEDGER.read_text())
 def calendar_db(self,rows):
  with sqlite3.connect(m.CALENDAR_DB) as c:
   c.execute('CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY,title TEXT)');c.execute('INSERT INTO Calendar VALUES (1,?)',(m.CALENDAR_NAME,));c.execute('INSERT INTO Calendar VALUES (2,?)',('其他日历',))
   c.execute('CREATE TABLE CalendarItem (UUID TEXT,summary TEXT,description TEXT,calendar_id INTEGER)');c.executemany('INSERT INTO CalendarItem VALUES (?,?,?,?)',rows)
 def fake_process(self,extra_error=False,bad_hash=False,missing_alarm=False,sync_remote=None):
  def run(args,**kwargs):
   from types import SimpleNamespace
   if args[0]=='/usr/bin/osascript':
    plan=json.loads(Path(args[-2]).read_text());self.last_plan=plan;e=plan['events'][0]
    rec={'uid':'fixed-uid','title':e['title'],'start':e['start'],'end':e['end'],'start_utc':e['start'],'end_utc':e['end'],'location':e['location'],'notes':e['notes'],'all_day':True,'match_count':1,'metadata_verified':True,'verified':False}
    Path(args[-1]).write_text(json.dumps({e['event_key']:rec}))
    return SimpleNamespace(returncode=0,stdout='',stderr='')
   p=json.loads(Path(args[-1]).read_text())
   if extra_error:return SimpleNamespace(returncode=75,stdout='',stderr='attachment not yet readable')
   value={'uid':p['uid'],'event_key':p['event_key'],'extras_verified':True,'metadata_preserved':True,'attachments':[{'source_path':x['source_path'],'sha256':'wrong' if bad_hash else x['sha256'],'content_verified':True} for x in p['attachment_manifest']],'alarms':[] if missing_alarm else [{'relative_minutes':-10080,'absolute_timestamp':None}]}
   if sync_remote is not None:
    value.update(attachment_sync_scope='icloud',attachment_sync_state='uploaded' if sync_remote else 'pending',sync_pending=not sync_remote,attachment_sync_evidence='remote_url_registered')
    for attachment in value['attachments']:attachment['remote_available']=sync_remote
   return SimpleNamespace(returncode=0,stdout=json.dumps(value),stderr='')
  return run
 def test_missing_pdf_stops_before_calendar(self):
  e=self.event();e['attachment_paths']=[]
  with self.assertRaisesRegex(AssertionError,'PDF'):m.record_review(self.docid,self.decision(e))
 def test_same_name_files_have_distinct_content_identity(self):
  other=self.root/'batch2';other.mkdir();p=other/self.pdf.name;p.write_bytes(b'%PDF-1.4\ndifferent date\n%%EOF')
  e=self.event();e['attachment_paths'].append(str(p));manifest=m.calendar_event_requirements(e)
  self.assertEqual(len(manifest),2);self.assertNotEqual(manifest[0]['sha256'],manifest[1]['sha256'])
 def test_internal_check_cannot_create_calendar_event(self):
  e=self.event();e['basis_type']='internal_check'
  with self.assertRaisesRegex(AssertionError,'待核'):m.calendar_event_requirements(e)
  e.update(metadata_only=True,existing_uid='known-uid');self.assertEqual(len(m.calendar_event_requirements(e)),1)
 def test_deadline_requires_seven_day_alarm(self):
  e=self.event();e.pop('alarm_minutes')
  with self.assertRaisesRegex(AssertionError,'7'):m.calendar_event_requirements(e)
 def test_old_saved_ready_does_not_write(self):
  state=self.state();state['documents'][self.docid].update(review_status='ready',decision=self.decision());state['documents'][self.docid]['decision'].pop('calendar_policy_version');m.atomic(m.LEDGER,state)
  with mock.patch('subprocess.run') as run:
   with self.assertRaisesRegex(AssertionError,'旧版'):m.apply_calendar(self.docid)
   run.assert_not_called()
 def test_attachment_failure_keeps_ready_and_existing_uid(self):
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(extra_error=True)):
   with self.assertRaises(RuntimeError):m.apply_calendar(self.docid)
  doc=self.state()['documents'][self.docid]
  self.assertEqual(doc['review_status'],'ready');self.assertEqual(doc['partial_receipts'][self.key]['uid'],'fixed-uid');self.assertFalse(doc['partial_receipts'][self.key]['verified'])
  with mock.patch('subprocess.run',side_effect=self.fake_process()) as run:m.apply_calendar(self.docid)
  self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0][1],'review-inspect')
  self.assertEqual(self.state()['documents'][self.docid]['review_status'],'complete')
  self.assertEqual(self.state()['events'][self.key]['uid'],'fixed-uid')
 def test_partial_recovery_inspection_failure_never_rewrites_or_adds(self):
  from types import SimpleNamespace
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(extra_error=True)):
   with self.assertRaises(RuntimeError):m.apply_calendar(self.docid)
  before=copy.deepcopy(self.state()['documents'][self.docid]['partial_receipts'])
  for code,kind in ((70,'calendar_metadata_changed'),(75,'attachment_content_unverified'),(77,'calendar_permission_unavailable'),(80,'duplicate_attachment_guard')):
   with self.subTest(code=code),mock.patch('subprocess.run',return_value=SimpleNamespace(returncode=code,stdout='',stderr='private raw provider text')) as run:
    with self.assertRaises(RuntimeError):m.apply_calendar(self.docid)
    self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0][1],'review-inspect')
   doc=self.state()['documents'][self.docid]
   self.assertEqual(doc['review_status'],'ready');self.assertEqual(doc['partial_receipts'],before)
   error=doc['calendar_recovery_errors'][self.key]
   self.assertEqual(error['kind'],kind);self.assertEqual(error['helper_exit_code'],code)
   self.assertNotIn('private',json.dumps(error));self.assertNotIn(self.key,self.state()['events'])
 def test_partial_recovery_wrong_sha_cannot_complete_or_fall_back_to_write(self):
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(extra_error=True)):
   with self.assertRaises(RuntimeError):m.apply_calendar(self.docid)
  with mock.patch('subprocess.run',side_effect=self.fake_process(bad_hash=True)) as run:
   with self.assertRaisesRegex(AssertionError,'附件'):m.apply_calendar(self.docid)
   self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0][1],'review-inspect')
  doc=self.state()['documents'][self.docid]
  self.assertEqual(doc['review_status'],'ready');self.assertFalse(doc['partial_receipts'][self.key]['verified'])
  self.assertEqual(doc['calendar_recovery_errors'][self.key]['kind'],'invalid_inspection_receipt')
 def test_partial_plan_metadata_conflict_stops_before_calendar_calls(self):
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(extra_error=True)):
   with self.assertRaises(RuntimeError):m.apply_calendar(self.docid)
  state=self.state();state['documents'][self.docid]['partial_receipts'][self.key]['notes']+='\n人工新增内容';m.atomic(m.LEDGER,state)
  with mock.patch('subprocess.run') as run:
   with self.assertRaisesRegex(AssertionError,'中断回执'):m.apply_calendar(self.docid)
   run.assert_not_called()
  self.assertIn('人工新增内容',self.state()['documents'][self.docid]['partial_receipts'][self.key]['notes'])
 def test_wrong_content_cannot_complete(self):
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(bad_hash=True)):
   with self.assertRaisesRegex(AssertionError,'附件'):m.apply_calendar(self.docid)
  self.assertEqual(self.state()['documents'][self.docid]['review_status'],'ready')
 def test_sync_pending_preserves_local_verification_and_readonly_refresh_never_adds(self):
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(sync_remote=False)):m.apply_calendar(self.docid)
  before=self.state();self.assertTrue(before['events'][self.key]['verified']);self.assertTrue(before['events'][self.key]['sync_pending'])
  with mock.patch('subprocess.run',side_effect=self.fake_process(sync_remote=True)) as run:result=m.refresh_attachment_sync(self.key)
  self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0][1],'review-inspect')
  self.assertFalse(result['sync_pending']);self.assertEqual(result['attachment_sync_state'],'uploaded')
  self.assertEqual(self.state()['documents'][self.docid]['review_status'],before['documents'][self.docid]['review_status'])
  self.assertEqual(self.state()['events'][self.key]['uid'],'fixed-uid')
 def test_cloud_status_requires_remote_evidence_and_local_has_no_cloud_state(self):
  event=self.event();event['attachment_manifest']=m.calendar_event_requirements(event)
  rec={'extras_verified':True,'metadata_preserved':True,'attachments':[{**event['attachment_manifest'][0],'content_verified':True,'remote_available':False}], 'alarms':[{'relative_minutes':-10080,'absolute_timestamp':None}],'attachment_sync_scope':'icloud','attachment_sync_state':'uploaded','sync_pending':False,'attachment_sync_evidence':'remote_url_registered'}
  with self.assertRaisesRegex(AssertionError,'云端状态'):m.verify_extras_receipt(event,rec)
  rec['attachment_sync_scope']='local'
  with self.assertRaisesRegex(AssertionError,'本地'):m.verify_extras_receipt(event,rec)
  for field in ('attachment_sync_state','sync_pending','attachment_sync_evidence'):rec.pop(field,None)
  m.verify_extras_receipt(event,rec)
 def test_missing_alarm_cannot_complete(self):
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(missing_alarm=True)):
   with self.assertRaisesRegex(AssertionError,'提醒'):m.apply_calendar(self.docid)
  self.assertEqual(self.state()['documents'][self.docid]['review_status'],'ready')
 def test_attachment_changed_after_review_stops_before_calendar(self):
  m.record_review(self.docid,self.decision());self.pdf.write_bytes(b'%PDF-1.4\nchanged\n%%EOF')
  with mock.patch('subprocess.run') as run:
   with self.assertRaisesRegex(AssertionError,'发生变化'):m.apply_calendar(self.docid)
   run.assert_not_called()
 def test_metadata_repair_can_review_existing_past_event(self):
  e=self.event();e.update(start='2020-01-01T00:00:00+08:00',end='2020-01-02T00:00:00+08:00',due_date='2020-01-01',metadata_only=True,existing_uid='known-uid')
  m.record_review(self.docid,self.decision(e));self.assertEqual(self.state()['documents'][self.docid]['review_status'],'ready')
  e.pop('metadata_only')
  with self.assertRaisesRegex(AssertionError,'过去'):m.record_review(self.docid,self.decision(e))
 def test_historical_receipt_creates_without_deadline_alarm(self):
  event=self.receipt_event();m.record_review(self.docid,self.decision(event))
  with mock.patch('subprocess.run',side_effect=self.fake_process(missing_alarm=True)):m.apply_calendar(self.docid)
  self.assertEqual(self.state()['documents'][self.docid]['review_status'],'complete')
 def test_unknown_deadline_can_keep_receipt_without_inventing_due_date(self):
  event=self.receipt_event();event.update(due_dates=[],pending_deadlines=['举证期限起算尚待核实'])
  m.record_review(self.docid,self.decision(event));self.assertEqual(self.state()['documents'][self.docid]['review_status'],'ready')
 def test_receipt_rejects_unbound_or_missing_evidence(self):
  for field,value in (('source_rowid',2),('source_doc','other-doc'),('document_sha256','0'*64),('binding_kind','case_sms_week'),('binding_source','')):
   with self.subTest(field=field):
    event=self.receipt_event();event['received_evidence'][field]=value
    with self.assertRaises(AssertionError):m.record_review(self.docid,self.decision(event))
 def test_received_date_cannot_be_processing_date(self):
  event=self.receipt_event();event['received_date']='2026-01-03'
  with self.assertRaisesRegex(AssertionError,'收到日期'):m.record_review(self.docid,self.decision(event))
 def test_repeat_delivery_keeps_first_proven_received_node(self):
  with mock.patch.object(m,'wake_independent_worker'):m.enqueue_documents(self.case,[self.pdf],'2026-01-03T12:00:00+08:00',2)
  event=self.receipt_event();m.record_review(self.docid,self.decision(event))
  event.update(received_date='2026-01-03',start='2026-01-03T00:00:00+08:00',end='2026-01-04T00:00:00+08:00');event['received_evidence'].update(source_rowid=2,received_at='2026-01-03T12:00:00+08:00')
  with self.assertRaisesRegex(AssertionError,'收件绑定'):m.record_review(self.docid,self.decision(event))
 def test_receipt_rejects_loose_due_dates_and_unstable_key(self):
  event=self.receipt_event();event['due_dates']=['2026-01-03']
  with self.assertRaisesRegex(AssertionError,'到期事项'):m.record_review(self.docid,self.decision(event))
  event=self.receipt_event();event['event_key']=self.case+'|收到材料|模型标题'
  with self.assertRaisesRegex(AssertionError,'稳定'):m.record_review(self.docid,self.decision(event))
 def test_past_deadline_requires_explicit_repair_and_verified_pair(self):
  event=self.event();event.update(start='2026-02-01T00:00:00+08:00',end='2026-02-02T00:00:00+08:00',due_date='2026-02-01')
  with self.assertRaisesRegex(AssertionError,'过去'):m.record_review(self.docid,self.decision(event))
  event['retrospective_pair']=True;m.record_review(self.docid,self.decision(event))
  event['paired_receipt_key']='unrelated'
  with self.assertRaisesRegex(AssertionError,'关联'):m.record_review(self.docid,self.decision(event))
 def test_past_appointment_cannot_use_pair_flag(self):
  event=self.event();event.update(start='2026-02-01T00:00:00+08:00',end='2026-02-02T00:00:00+08:00',event_kind='appointment',is_deadline=False,retrospective_pair=True)
  with self.assertRaisesRegex(AssertionError,'过去'):m.record_review(self.docid,self.decision(event))
 def test_deadline_requires_matching_date_metadata(self):
  for key in ('received_date','received_evidence','paired_receipt_key','due_date'):
   with self.subTest(key=key):
    event=self.event();event.pop(key)
    with self.assertRaises(AssertionError):m.record_review(self.docid,self.decision(event))
  event=self.event();event['due_date']='2026-01-03'
  with self.assertRaisesRegex(AssertionError,'等于'):m.record_review(self.docid,self.decision(event))
 def test_v2_ready_cannot_resume_or_be_approved(self):
  decision=self.decision();decision['calendar_policy_version']=2
  with self.assertRaisesRegex(AssertionError,'旧版'):m.record_review(self.docid,decision)
  state=self.state();state['documents'][self.docid].update(review_status='ready',decision=decision);m.atomic(m.LEDGER,state)
  with mock.patch('subprocess.run') as run:
   with self.assertRaisesRegex(AssertionError,'旧版'):m.apply_calendar(self.docid)
   run.assert_not_called()
 def test_week_level_legacy_association_requires_exact_proof(self):
  state=self.state();doc=state['documents'][self.docid];doc['scope_evidence']={'receipt_precision':'case_sms_week'};doc.pop('receipt_binding_verified');m.atomic(m.LEDGER,state)
  event=self.receipt_event()
  with self.assertRaisesRegex(AssertionError,'旧同案周'):m.record_review(self.docid,self.decision(event))
  doc.update(receipt_binding_verified=True,receipt_binding=self.evidence());m.atomic(m.LEDGER,state)
  m.record_review(self.docid,self.decision(event))
  state=self.state();state['documents'][self.docid]['receipt_binding']['source_rowid']=99;m.atomic(m.LEDGER,state)
  with mock.patch('subprocess.run') as run:
   with self.assertRaisesRegex(AssertionError,'旧同案周'):m.apply_calendar(self.docid)
   run.assert_not_called()
 def test_unverified_older_week_delivery_does_not_override_verified_binding(self):
  state=self.state();doc=state['documents'][self.docid];doc['deliveries'].append({'received_at':'2026-01-01T12:00:00+08:00','source_rowid':99});doc['scope_evidence']={'receipt_precision':'case_sms_week'};m.atomic(m.LEDGER,state)
  m.record_review(self.docid,self.decision(self.receipt_event()));self.assertEqual(self.state()['documents'][self.docid]['review_status'],'ready')
 def test_receipt_only_preserves_pending_main_review_and_ocr_state(self):
  state=self.state();doc=state['documents'][self.docid];doc.update(ocr_status='pending',review_status='attention',events=['older-event'],decision={'conclusion':'期限待核','unresolved':['等待完整审查']});doc.pop('ocr');m.atomic(m.LEDGER,state)
  m.record_receipt_review(self.docid,self.receipt_decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(missing_alarm=True)):m.apply_calendar(self.docid,receipt_only=True)
  doc=self.state()['documents'][self.docid]
  self.assertEqual(doc['ocr_status'],'pending');self.assertNotIn('ocr',doc);self.assertEqual(doc['review_status'],'attention');self.assertEqual(doc['decision']['conclusion'],'期限待核');self.assertEqual(doc['events'],['older-event'])
  self.assertEqual(doc['receipt_review_status'],'complete');self.assertEqual(doc['receipt_events'],[self.receipt_key]);self.assertTrue(self.state()['events'][self.receipt_key]['verified'])
 def test_receipt_only_retry_retains_uid_and_main_status(self):
  m.record_receipt_review(self.docid,self.receipt_decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(extra_error=True)):
   with self.assertRaises(RuntimeError):m.apply_calendar(self.docid,receipt_only=True)
  doc=self.state()['documents'][self.docid];self.assertEqual(doc['review_status'],'pending');self.assertEqual(doc['receipt_review_status'],'ready');self.assertNotIn('partial_receipts',doc)
  with mock.patch('subprocess.run',side_effect=self.fake_process(missing_alarm=True)) as run:m.apply_calendar(self.docid,receipt_only=True)
  self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0][1],'review-inspect')
  self.assertEqual(self.state()['events'][self.receipt_key]['uid'],'fixed-uid');self.assertEqual(self.state()['documents'][self.docid]['review_status'],'pending')
 def test_receipt_only_cannot_claim_full_review_or_add_deadline(self):
  decision=self.receipt_decision();decision['all_pages_reviewed']=True
  with self.assertRaisesRegex(AssertionError,'全文'):m.record_receipt_review(self.docid,decision)
  decision=self.receipt_decision();decision['events'].append(self.event())
  with self.assertRaisesRegex(AssertionError,'一条'):m.record_receipt_review(self.docid,decision)
 def test_receipt_only_requires_visual_record_and_original_pdf(self):
  decision=self.receipt_decision();decision['material_page_review']['method']='ocr'
  with self.assertRaisesRegex(AssertionError,'原图'):m.record_receipt_review(self.docid,decision)
  decision=self.receipt_decision();decision['material_page_review']['page']=2
  with self.assertRaisesRegex(AssertionError,'页码'):m.record_receipt_review(self.docid,decision)
  decision=self.receipt_decision();Path(decision['material_page_review']['review_record']).unlink()
  with self.assertRaisesRegex(AssertionError,'核对记录'):m.record_receipt_review(self.docid,decision)
 def test_receipt_only_rechecks_visual_record_before_writing(self):
  decision=self.receipt_decision();m.record_receipt_review(self.docid,decision);Path(decision['material_page_review']['review_record']).write_text('changed')
  with mock.patch('subprocess.run') as run:
   with self.assertRaisesRegex(AssertionError,'记录已变化'):m.apply_calendar(self.docid,receipt_only=True)
   run.assert_not_called()
 def test_optional_sql_lookup_matches_exact_marker_and_calendar_without_writes(self):
  marker='法院材料事项ID：'+self.key
  self.calendar_db([('right','原题','人工备注\n'+marker,1),('prefix','其他题',marker+'额外事项',1),('other-calendar','其他日历',marker,2)])
  digest=m.digest(m.CALENDAR_DB);snapshot=m.calendar_marker_snapshot([self.event()])
  self.assertEqual(snapshot['by_event_key'][self.key],[{'uid':'right','title':'原题','notes':'人工备注\n'+marker}]);self.assertEqual(m.digest(m.CALENDAR_DB),digest)
 def test_sql_lookup_retains_duplicate_matches(self):
  marker='法院材料事项ID：'+self.key;self.calendar_db([('one','题1',marker,1),('two','题2',marker,1)])
  self.assertEqual(len(m.calendar_marker_snapshot([self.event()])['by_event_key'][self.key]),2)
 def test_unavailable_sql_lookup_falls_back_without_creating_database(self):
  self.assertIsNone(m.calendar_marker_snapshot([self.event()]));self.assertFalse(m.CALENDAR_DB.exists())
  m.record_review(self.docid,self.decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process()):m.apply_calendar(self.docid,fast_lookup=True)
  self.assertNotIn('trusted_calendar_lookup',self.last_plan)
 def test_invalid_sql_schema_falls_back(self):
  with sqlite3.connect(m.CALENDAR_DB) as c:c.execute('CREATE TABLE unrelated (id INTEGER)')
  self.assertIsNone(m.calendar_marker_snapshot([self.event()]))
 def test_sql_hint_is_generated_under_writer_lock_not_from_model(self):
  self.calendar_db([]);decision=self.decision();decision['trusted_calendar_lookup']={'forged':True};decision['events'][0]['trusted_calendar_lookup']={'forged':True};m.record_review(self.docid,decision)
  original=m.calendar_marker_snapshot
  def snapshot(events):
   import fcntl
   with (self.root/'calendar_review.lock').open('a+') as lock:
    with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   return original(events)
  with mock.patch.object(m,'calendar_marker_snapshot',side_effect=snapshot),mock.patch('subprocess.run',side_effect=self.fake_process()):m.apply_calendar(self.docid,fast_lookup=True)
  self.assertEqual(self.last_plan['trusted_calendar_lookup']['by_event_key'],{self.key:[]})
  self.assertNotIn('forged',self.last_plan['trusted_calendar_lookup'])
 def test_sql_hint_is_disabled_for_existing_callers(self):
  m.record_review(self.docid,self.decision())
  with mock.patch.object(m,'calendar_marker_snapshot') as snapshot,mock.patch('subprocess.run',side_effect=self.fake_process()):m.apply_calendar(self.docid)
  snapshot.assert_not_called();self.assertNotIn('trusted_calendar_lookup',self.last_plan)
 def test_v4_group_preserves_member_review_states_and_verifies_all_pdfs(self):
  decision=self.group_decision();before=copy.deepcopy(self.state()['documents'])
  m.record_receipt_group_review(self.receipt_key,decision)
  with mock.patch('subprocess.run',side_effect=self.fake_process(missing_alarm=True)):m.apply_receipt_group_calendar(self.receipt_key)
  state=self.state();self.assertEqual(state['documents'],before);self.assertEqual(len(state['events'][self.receipt_key]['attachments']),2);self.assertTrue(state['events'][self.receipt_key]['verified'])
 def test_group_rejects_different_case_or_received_day(self):
  decision=self.group_decision();second=decision['events'][0]['receipt_documents'][1]
  state=self.state();state['documents'][second['id']]['case_number']='（2026）测01民初888号';m.atomic(m.LEDGER,state)
  with self.assertRaisesRegex(AssertionError,'不同案号'):m.record_receipt_group_review(self.receipt_key,decision)
  state['documents'][second['id']]['case_number']=self.case;second['binding']['received_at']='2026-01-03T13:00:00+08:00';state['documents'][second['id']]['receipt_binding']=second['binding'];m.atomic(m.LEDGER,state)
  with self.assertRaisesRegex(AssertionError,'收到日期'):m.record_receipt_group_review(self.receipt_key,decision)
 def test_group_requires_every_member_pdf_and_cannot_shrink(self):
  decision=self.group_decision();event=decision['events'][0];paths=list(event['attachment_paths']);event['attachment_paths']=paths[:1]
  with self.assertRaisesRegex(AssertionError,'某份原件PDF'):m.record_receipt_group_review(self.receipt_key,decision)
  event['attachment_paths']=paths;m.record_receipt_group_review(self.receipt_key,decision)
  reduced=copy.deepcopy(decision);reduced['events'][0]['receipt_documents']=reduced['events'][0]['receipt_documents'][:1]
  with self.assertRaisesRegex(AssertionError,'遗漏已登记成员'):m.record_receipt_group_review(self.receipt_key,reduced)
 def test_group_reuses_uid_after_attachment_failure(self):
  m.record_receipt_group_review(self.receipt_key,self.group_decision())
  with mock.patch('subprocess.run',side_effect=self.fake_process(extra_error=True)):
   with self.assertRaises(RuntimeError):m.apply_receipt_group_calendar(self.receipt_key)
  with mock.patch('subprocess.run',side_effect=self.fake_process(missing_alarm=True)) as run:m.apply_receipt_group_calendar(self.receipt_key)
  self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0][1],'review-inspect')
  current=self.state();self.assertEqual(current['events'][self.receipt_key]['uid'],'fixed-uid')
  self.assertEqual(len(current['events'][self.receipt_key]['attachments']),2)
 def test_v3_ready_cannot_resume_after_grouping_upgrade(self):
  decision=self.decision();decision['calendar_policy_version']=3
  with self.assertRaisesRegex(AssertionError,'旧版'):m.record_review(self.docid,decision)
 def test_group_member_can_reference_previously_verified_legacy_receipt(self):
  decision=self.receipt_decision();member=decision['events'][0]['receipt_documents'][0];old_key=self.case+'|收到材料|'+m.digest(self.pdf)
  old_event=self.receipt_event();old_event['event_key']=old_key
  state=self.state();state['documents'][self.docid]['receipt_decision']={'events':[old_event]};state['events'][old_key]={'uid':'old-uid','verified':True,'extras_verified':True,'attachments':[{'sha256':m.digest(self.pdf),'content_verified':True}]};m.atomic(m.LEDGER,state)
  member.update(review_basis='verified_receipt',receipt_event_key=old_key);member.pop('material_page_review')
  m.record_receipt_group_review(self.receipt_key,decision)
  state=self.state();state['events'][old_key]['attachments']=[];m.atomic(m.LEDGER,state)
  with self.assertRaisesRegex(AssertionError,'附件未核验'):m.record_receipt_group_review(self.receipt_key,decision)

if __name__=='__main__':unittest.main()
