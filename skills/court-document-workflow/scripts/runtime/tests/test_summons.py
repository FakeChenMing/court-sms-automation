import unittest,types,pathlib,logging,unittest.mock as mock
p=pathlib.Path(__file__).resolve().parents[1]/'court_sms_monitor.py'
m=types.ModuleType('monitor');m.__file__=str(p)
exec(compile(p.read_text().replace('LOGGER = setup_logging()','LOGGER = logging.getLogger("test")'),str(p),'exec'),m.__dict__)
class Tests(unittest.TestCase):
 def test_purpose_not_person(self):
  for name in ['传票（证据交换）.pdf','传票(庭前会议).pdf','传票（开庭）.pdf']:
   self.assertEqual(m.party_from_filename(pathlib.Path(name)),'')
 def test_real_person_preserved(self):
  self.assertEqual(m.party_from_filename(pathlib.Path('传票（张三）.pdf')),'张三')
  self.assertEqual(m.party_from_filename(pathlib.Path('传票（证据交换）张三.pdf')),'张三')
 def text(self,time='09:30'):
  return '示例市测试区人民法院案由侵害经营秘密纠纷被传唤人姓名张三单位或住址某地被传事由证据交换应到时间2026年12月04日'+time+'应到处所第二十三法庭注意事项'
 def test_evidence_exchange_title(self):
  with mock.patch.object(m,'extract_pdf_text',return_value=self.text()):
   e=m.parse_summons(pathlib.Path('传票（证据交换）.pdf'),'测试案号')
  self.assertEqual(e['title'],'测试案号 证据交换');self.assertEqual(e['start'],'2026-12-04T09:30:00')
 def test_distinct_times_not_collapsed(self):
  with mock.patch.object(m,'extract_pdf_text',return_value=self.text()+self.text('10:30')):
   self.assertIsNone(m.parse_summons(pathlib.Path('传票（证据交换）.pdf'),'测试案号'))
 def test_real_name_conflict_still_rejected(self):
  with mock.patch.object(m,'extract_pdf_text',return_value=self.text()):
   self.assertIsNone(m.parse_summons(pathlib.Path('传票（李四）.pdf'),'测试案号'))

 def test_combined_same_slot_with_instructions(self):
  text=(self.text()+"被传人必须到达应到处所。法院另行通知。")*5
  with mock.patch.object(m,'extract_pdf_text',return_value=text):
   self.assertIsNotNone(m.parse_summons(pathlib.Path('传票（证据交换）.pdf'),'测试案号'))

 def test_division_marker_not_party(self):
  for name in ['传票（民一）合.pdf','传票(民二庭)合并.pdf','传票（民一）.pdf']:
   self.assertEqual(m.party_from_filename(pathlib.Path(name)),'')
  self.assertEqual(m.party_from_filename(pathlib.Path('传票（民一）张三.pdf')),'张三')
 def test_investigation_is_not_hearing(self):
  text=self.text().replace('被传事由证据交换','传唤事由调查')
  with mock.patch.object(m,'extract_pdf_text',return_value=text):
   e=m.parse_summons(pathlib.Path('传票（民一）合.pdf'),'测试案号')
  self.assertEqual(e['title'],'测试案号 调查')
  self.assertIn('事项：调查',e['description'])
