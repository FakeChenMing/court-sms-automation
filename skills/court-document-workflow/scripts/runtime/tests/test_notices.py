import sys,pathlib,unittest,types,logging,datetime,json
from unittest import mock
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from court_schedule_notices import parse_notice,named_candidate
p=ROOT/'court_sms_monitor.py';m=types.ModuleType('notice_monitor');m.__file__=str(p)
exec(compile(p.read_text().replace('LOGGER = setup_logging()','LOGGER = logging.getLogger("test")'),str(p),'exec'),m.__dict__)
CASE='（2026）测0114民初90001号';NOW=datetime.datetime(2026,11,12,12)
class NoticeTests(unittest.TestCase):
 def text(self):return '关于涉案工程进场鉴定的告知函。本司拟于2026年11月20日上午10:00进入某公司车间项目厂房，按照方案开展现场检测鉴定。'
 def parse(self,t=None,name='进场鉴定告知函.pdf'):
  return parse_notice(t or self.text(),pathlib.Path(name),CASE,NOW)
 def test_firm_narrative(self):
  e=self.parse(self.text().replace('拟于','定于'));self.assertEqual(e['status'],'event');self.assertEqual(e['event']['location'],'某公司车间项目厂房')
 def test_actual_style_will_enter(self):
  e=self.parse(self.text().replace('拟于','将于'));self.assertEqual(e['status'],'event')
 def test_tentative_is_manual(self):self.assertEqual(self.parse()['status'],'manual')
 def test_labels(self):
  e=self.parse('听证通知书。听证时间：2026年11月20日下午2时30分。听证地点：第三法庭。','听证通知书.pdf')
  self.assertEqual(e['event']['start'],'2026-11-20T14:30:00');self.assertEqual(e['event']['location'],'第三法庭')
 def test_weekday(self):
  e=self.parse('进场鉴定通知。我司定于2026年11月20日（周五）上午10:00进入某项目厂房，进行现场鉴定。');self.assertEqual(e['status'],'event')
 def test_past_no_venue_needed(self):self.assertEqual(self.parse('进场鉴定通知。定于2026年11月9日上午10:00。')['status'],'past')
 def test_today_elapsed(self):self.assertEqual(self.parse('进场鉴定通知。定于2026年11月12日上午10:00。')['status'],'past')
 def test_today_future(self):self.assertEqual(self.parse('进场鉴定通知。定于2026年11月12日下午2时进入某项目厂房，进行鉴定。')['status'],'event')
 def test_relative_time_not_guessed(self):self.assertEqual(self.parse('进场鉴定通知。请于明日上午10:00前往项目厂房。')['status'],'manual')
 def test_missing_time(self):self.assertEqual(self.parse('进场鉴定通知。定于2026年11月20日进入项目厂房。')['status'],'manual')
 def test_missing_location(self):self.assertEqual(self.parse('进场鉴定通知。定于2026年11月20日10:00。')['status'],'manual')
 def test_invalid_date(self):self.assertEqual(self.parse('进场鉴定通知。定于2026年11月31日10:00。')['status'],'manual')
 def test_multiple_slots(self):self.assertEqual(self.parse(self.text().replace('拟于','定于')+'另定于2026年11月21日10:00进入另厂房，进行鉴定。')['status'],'manual')
 def test_multiple_places(self):self.assertEqual(self.parse(self.text().replace('拟于','定于')+'地点：另一个厂房。')['status'],'manual')
 def test_cancelled(self):self.assertEqual(self.parse(self.text().replace('拟于','定于')+'现取消。')['status'],'manual')
 def test_unknown_case(self):self.assertEqual(parse_notice(self.text().replace('拟于','定于'),pathlib.Path('进场鉴定通知.pdf'),'待核案号',NOW)['status'],'manual')
 def test_stamp_date_not_event(self):self.assertEqual(self.parse('进场鉴定告知函。请查收。落款2026年11月20日10:00')['status'],'manual')
 def test_generic_filename_content(self):self.assertEqual(self.parse(self.text().replace('拟于','定于'),'文书.pdf')['status'],'event')
 def test_static_notice_not_event(self):self.assertEqual(self.parse('如需听证，时间：2026年11月20日10:00。地点：法庭。','权利义务告知书.pdf')['status'],'irrelevant')
 def test_ocr_failure_kept(self):
  with mock.patch.object(m,'extract_pdf_text',return_value=''),mock.patch.object(m,'extract_mineru_fields',return_value={}):
   self.assertEqual(m.inspect_schedule_document(pathlib.Path('进场鉴定通知.pdf'),CASE)['status'],'manual')
 def test_notice_enters_retry_queue(self):
  state={};m.queue_pending_summons(state,CASE,[pathlib.Path('进场鉴定通知.pdf')]);self.assertEqual(len(state['pending_summons']),1)

 def test_tentative_word_with_explicit_attendance_instruction(self):
  t=self.text()+'请贵院通知涉案双方准时到场见证。'
  self.assertEqual(self.parse(t)['status'],'event')

 def test_numeric_date(self):
  t='听证通知书。时间：2026-11-20下午2:30。地点：第三法庭。'
  self.assertEqual(self.parse(t,'听证通知书.pdf')['event']['start'],'2026-11-20T14:30:00')

 def test_legacy_calendar_write_is_disabled_without_side_effects(self):
  with mock.patch.object(m.subprocess,"run") as run,mock.patch.object(m,"add_manual_reminder") as reminder:
   with self.assertRaisesRegex(RuntimeError,"旧排期直写入口已禁用"):
    m.add_calendar_from_summons(CASE,[pathlib.Path("通知.pdf")],{},False)
   run.assert_not_called();reminder.assert_not_called()
