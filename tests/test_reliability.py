import unittest,tempfile,pathlib,types,unittest.mock as mock,json,datetime
source=(pathlib.Path(__file__).resolve().parents[1]/'scripts/runtime/court_sms_monitor.py').read_text().replace('LOGGER = setup_logging()', 'LOGGER = logging.getLogger("repair-test")\nLOGGER.addHandler(logging.NullHandler())\nLOGGER.propagate = False')
m=types.ModuleType('repair');m.__file__=str(pathlib.Path(__file__).resolve().parents[1]/'scripts/runtime/court_sms_monitor.py');exec(compile(source,m.__file__,'exec'),m.__dict__)
class RepairTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
  m.MIN_RECEIVED_AT=datetime.datetime(2026,1,1)
  m.CONFIG["calendar_enabled"]=True
  m.CONFIG["start_date"]="2026-01-01"
  m.CONFIG_PATH=self.root/"config.json";m.CONFIG_PATH.write_text("{}")
  m.LOG_PATH=self.root/"monitor.log"
  m.LOGGER.handlers.clear();m.LOGGER.addHandler(m.logging.NullHandler());m.LOGGER.propagate=False
  m.STATE_DIR=self.root;m.STATE_PATH=self.root/'state.json';m.BACKUP_PATH=self.root/'backup.json';m.ROOT=self.root;m.LOCK_PATH=self.root/'lock'
 def tearDown(self):self.tmp.cleanup()
 def state(self):return m.validate_state({'last_rowid':100})
 def test_missing_state_stops(self):
  with self.assertRaises(RuntimeError):m.load_state()
 def test_corrupt_primary_recovers_backup(self):
  m.STATE_PATH.write_text('{bad');m.BACKUP_PATH.write_text(json.dumps(self.state()));self.assertEqual(m.load_state()['last_rowid'],100)
 def test_save_keeps_all_pending(self):
  s=self.state();s['pending_summons']=[{'path':str(i)} for i in range(220)];m.save_state(s);self.assertEqual(len(m.load_state()['pending_summons']),220)
 def test_failure_persisted_and_cursor_advances_safely(self):
  m.save_state(self.state());date=(datetime.datetime(2026,10,15)-datetime.datetime(2001,1,1)).total_seconds()*1e9
  with mock.patch.object(m,'fetch_new_messages',return_value=[{'rowid':101,'date':date,'text':'法院'}]),mock.patch.object(m,'process_message',side_effect=TimeoutError()),mock.patch.object(m,'retry_pending_reports'),mock.patch.object(m,'retry_pending_summons'),mock.patch.object(m,'retry_pending_reminders'):
   self.assertEqual(m.main(),0)
  s=m.load_state();self.assertEqual(s['last_rowid'],101);self.assertEqual(s['message_jobs']['101']['status'],'retry');self.assertEqual(s['message_jobs']['101']['attempts'],1)
 def test_old_receipt_never_processed(self):
  m.save_state(self.state());date=(datetime.datetime(2025,10,15)-datetime.datetime(2001,1,1)).total_seconds()*1e9
  with mock.patch.object(m,'fetch_new_messages',return_value=[{'rowid':101,'date':date,'text':'法院'}]),mock.patch.object(m,'process_message') as process,mock.patch.object(m,'retry_pending_reports'),mock.patch.object(m,'retry_pending_summons'),mock.patch.object(m,'retry_pending_reminders'):
   m.main();process.assert_not_called()
 def test_bad_host_not_accepted(self):self.assertIsNone(m.parse_zxfw_params('https://notcourt.gov.cn/#/pagesAjkj/app/wssd/index?qdbh=a&sdbh=b'))
 def test_url_chinese_suffix(self):self.assertEqual(m.extract_urls('法院 https://example.org/a）查阅文书。'),['https://example.org/a'])
 def test_download_failure_raises(self):
  s=self.state();msg={'rowid':101,'date':0,'text':'人民法院 https://zxfw.court.gov.cn/#/pagesAjkj/app/wssd/index?qdbh=a&sdbh=b'}
  with mock.patch.object(m,'handle_zxfw_delivery',side_effect=TimeoutError()),mock.patch.object(m,'identify_case_cause',return_value=''):
   with self.assertRaises(RuntimeError):m.process_message(msg,s)
  self.assertEqual(len(s['pending_reports']),1);self.assertEqual(s['downloaded_urls'],[])
 def test_failed_report_batch_keeps_queue(self):
  s=self.state();s['pending_reports']=[{'report_id':'sms-101','title':'x','body':'y','attempts':0,'next_retry_at':''}]
  with mock.patch.object(m.subprocess,'run',side_effect=TimeoutError()):m.retry_pending_reports(s)
  self.assertEqual(len(s['pending_reports']),1);self.assertEqual(s['pending_reports'][0]['attempts'],1)
 def test_partial_native_receipt_preserves_unsent(self):
  s=self.state();s['pending_reports']=[{'report_id':'sms-'+str(i),'title':'x','body':'y','attempts':0,'next_retry_at':''} for i in [101,102]]
  def run(command,**kwargs):
   pathlib.Path(command[2]).write_text(json.dumps({'confirmed':['sms-102'],'receipts':{'sms-102':'native-id'}}))
   return types.SimpleNamespace(returncode=72)
  with mock.patch.object(m.subprocess,'run',side_effect=run):m.retry_pending_reports(s)
  self.assertEqual([i['report_id'] for i in s['pending_reports']],['sms-101']);self.assertIn('sms-102',s['report_receipts'])
 def test_summons_bounded_retry_becomes_visible_manual(self):
  s=self.state();file=self.root/'传票.pdf';file.write_bytes(b'test');s['pending_summons']=[{'path':str(file),'attempts':5,'next_retry_at':'','case_number':'test'}]
  with mock.patch.object(m,'add_calendar_from_summons',side_effect=RuntimeError()):m.retry_pending_summons(s)
  self.assertEqual(s['pending_summons'],[]);self.assertEqual(len(s['manual_review']),1);self.assertEqual(len(s['pending_reports']),1)
 def test_same_case_same_filename_different_delivery(self):
  import io,hashlib
  item={'c_wsmc':'传票','c_wjgs':'pdf','c_wsbh':'doc1','wjlj':'https://example.org/doc'}
  paths=[]
  for delivery,content in [('a',b'%PDF-first'),('b',b'%PDF-second')]:
   url='https://zxfw.court.gov.cn/#/pagesAjkj/app/wssd/index?qdbh=q&sdbh='+delivery
   with mock.patch.object(m,'http_json',return_value={'code':200,'data':[item]}),mock.patch.object(m,'urlopen',return_value=io.BytesIO(content)):
    new,available,_=m.handle_zxfw_delivery(url,'same-case',datetime.datetime(2026,10,15))
   paths.append(available[0]);self.assertEqual(available[0].read_bytes(),content)
   manifest=json.loads((available[0].parent.parent/'.delivery_manifest.json').read_text());self.assertEqual(manifest['documents'][0]['sha256'],hashlib.sha256(content).hexdigest())
  self.assertNotEqual(paths[0],paths[1]);self.assertTrue(all(p.exists() for p in paths))
 def test_same_delivery_same_name_distinct_document_ids(self):
  import io
  items=[{'c_wsmc':'传票','c_wjgs':'pdf','c_wsbh':str(i),'wjlj':'https://example.org/doc'} for i in [1,2]]
  with mock.patch.object(m,'http_json',return_value={'code':200,'data':items}),mock.patch.object(m,'urlopen',side_effect=[io.BytesIO(b'%PDF-one'),io.BytesIO(b'%PDF-two')]):
   _,files,_=m.handle_zxfw_delivery('https://zxfw.court.gov.cn/#/pagesAjkj/app/wssd/index?qdbh=q&sdbh=a','case',datetime.datetime(2026,10,15))
  self.assertEqual(len(set(files)),2);self.assertNotEqual(files[0].read_bytes(),files[1].read_bytes())
 def test_changed_remote_file_preserves_prior_content(self):
  import io
  p=self.root/'doc.pdf';p.write_bytes(b'%PDF-old')
  with mock.patch.object(m,'urlopen',return_value=io.BytesIO(b'%PDF-new')):self.assertTrue(m.download_file('https://example.org/doc',p))
  self.assertEqual(p.read_bytes(),b'%PDF-new');self.assertEqual(next((self.root/'.versions').rglob('doc.pdf')).read_bytes(),b'%PDF-old')
 def test_existing_file_does_not_hide_invalid_remote_pdf(self):
  import io
  p=self.root/'doc.pdf';p.write_bytes(b'%PDF-existing')
  with mock.patch.object(m,'urlopen',return_value=io.BytesIO(b'<html>error</html>')):
   with self.assertRaises(ValueError):m.download_file('https://example.org/doc',p)
  self.assertEqual(p.read_bytes(),b'%PDF-existing')
if __name__=='__main__':unittest.main()
