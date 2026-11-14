import sys,unittest,copy
from pathlib import Path
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from workflow_worker import validate,TZ,parse_response
class Gates(unittest.TestCase):
 def setUp(self):
  self.case='（2026）测0114民初90001号';d=datetime.now(TZ)+timedelta(days=10);self.start=d.replace(hour=10,minute=0,second=0,microsecond=0)
  self.quote=f'定于{d.year}年{d.month}月{d.day}日10时在某厂进场鉴定。'
  self.docs=[{'id':'a','pages':[{'text':self.quote+'原告甲公司，被告乙公司。'}]}]
  self.result={'case_number':self.case,'documents':[{'id':'a','reviewed_pages':[1],'date_inventory':[{'source_page':1,'source_quote':self.quote}],'conclusion':'已审查'}],'parties':[{'role':'原告','name':'甲公司','source_doc':'a','source_page':1,'source_quote':'原告甲公司'}],'events':[{'owner_doc':'a','source_page':1,'source_quote':self.quote,'basis_type':'document_explicit','start':self.start.isoformat(),'end':(self.start+timedelta(hours=1)).isoformat(),'all_day':False,'title':self.case+' 进场鉴定','category':'进场鉴定','subject':'涉案工程'}]}
 def check(self):return validate(self.result,self.docs,['a'],self.case,[],[])
 def test_explicit_notice(self):self.check()
 def test_ocr_markdown_escape(self):
  from document_ocr import clean
  self.assertEqual(clean(r"2026-08-03\~2026-08-04"),clean("2026-08-03~2026-08-04"))
 def test_wrong_hour(self):
  self.result["events"][0]["start"]=(self.start+timedelta(hours=1)).isoformat();self.result["events"][0]["end"]=(self.start+timedelta(hours=2)).isoformat()
  with self.assertRaises(AssertionError):self.check()
 def test_prefaced_json(self):
  self.assertEqual(parse_response({"content":[{"type":"text","text":'核验结果：\n```json\n{"approved":true}\n```'}]}),{"approved":True})
 def test_linked_investigation_date(self):
  self.docs[0]['pages'][0]['text']+='二审不需要开庭的，应当在收到本通知书的次日起至调查日内提出。调查'
  e=self.result['events'][0];e['source_quote']='二审不需要开庭的，应当在收到本通知书的次日起至调查日内提出。';e['all_day']=True
  e['date_reference']={'source_doc':'a','source_page':1,'source_quote':self.quote}
  self.check()
  e['date_reference']['source_quote']='2027年1月1日调查'
  with self.assertRaises(AssertionError):self.check()
 def test_visual_transcript_bound_to_source(self):
  from workflow_worker import quoted
  d={'id':'a','source_sha256':'fixture-hash','pages':[{'text':'','source_visual_review':{'reviewed':True,'page':1,'source_sha256':'fixture-hash','transcription':'在开庭一日前预交'}}]}
  self.assertTrue(quoted({'a':d},'a',1,'在开庭一日前预交'))
  d['pages'][0]['source_visual_review']['source_sha256']='other'
  self.assertFalse(quoted({'a':d},'a',1,'在开庭一日前预交'))
 def test_secondary_ocr_quote_bound_to_source(self):
  from workflow_worker import quoted
  d={'source_sha256':'original','pages':[{'text':'','secondary_ocr':{'source_sha256':'original','page':1,'text':'被执行人甲公司'}}]}
  self.assertTrue(quoted({'a':d},'a',1,'被执行人甲公司'))
  d['pages'][0]['secondary_ocr']['source_sha256']='other'
  self.assertFalse(quoted({'a':d},'a',1,'被执行人甲公司'))
 def test_party_from_same_case_sms(self):
  self.docs[0]['same_case_messages']=[{'id':'123','received_at':'2026-11-01','text':self.case+'原告甲公司诉被告乙公司'}]
  self.result['parties']=[{'role':'被告','name':'乙公司','source_message_id':'123','source_quote':'原告甲公司诉被告乙公司'}]
  self.check()
  self.result['parties'][0]['source_message_id']='other-case'
  with self.assertRaises(AssertionError):self.check()
 def test_commit_refreshes_and_preserves_new_manual_notes(self):
  from unittest.mock import patch
  import tempfile,json
  import workflow_worker as w
  self.docs[0].update(source_sha256='source',source_path='/fixture.pdf')
  key=self.case+'|进场鉴定|涉案工程'
  existing=[{'uid':'existing','start_utc':self.start.isoformat(),'notes':'用户新加的备注\n\n【法院材料自动登记】\n法院材料事项ID：'+key}]
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'ledger.json').write_text(json.dumps({'version':1,'documents':{'a':{}},'events':{},'cases':{}}))
   with patch.multiple(w.ledger,STATE_DIR=root,LEDGER=root/'ledger.json'),patch.object(w,'STATE',root),patch.object(w,'calendar_read',return_value=existing),patch.object(w.ledger,'record_review') as record,patch.object(w.ledger,'apply_calendar'):
    w.commit(self.case,self.docs,['a'],self.result,[],[])
    event=record.call_args.args[1]['events'][0]
    self.assertEqual(event['existing_uid'],'existing')
    self.assertTrue(event['notes'].startswith('用户新加的备注'))
 def test_wrong_case(self):
  self.result['case_number']='（2025）测0114民初90001号'
  with self.assertRaises(AssertionError):self.check()
 def test_missing_page(self):
  self.docs[0]['pages'].append({'text':'另一页'})
  with self.assertRaises(AssertionError):self.check()
 def test_unproven_name(self):
  self.result['parties'][0]['name']='丙公司'
  with self.assertRaises(AssertionError):self.check()
 def test_missing_source(self):
  self.result['events'][0]['source_quote']='法院另定12月开庭'
  with self.assertRaises(AssertionError):self.check()
 def test_wrong_date(self):
  self.result['events'][0]['start']=(self.start+timedelta(days=1)).isoformat();self.result['events'][0]['end']=(self.start+timedelta(days=1,hours=1)).isoformat()
  with self.assertRaises(AssertionError):self.check()
 def test_past_forbidden(self):
  self.result['events'][0]['start']=(self.start-timedelta(days=20)).isoformat();self.result['events'][0]['end']=(self.start-timedelta(days=20)+timedelta(hours=1)).isoformat()
  with self.assertRaises(AssertionError):self.check()
 def test_internal_must_label(self):
  self.result['events'][0]['basis_type']='internal_check';self.result['events'][0]['uncertainty']='送达日未知'
  with self.assertRaises(AssertionError):self.check()
 def test_duplicate_rejected(self):
  self.result['events'].append(copy.deepcopy(self.result['events'][0]))
  with self.assertRaises(AssertionError):self.check()
 def test_calculation_without_anchor_rejected(self):
  self.result['events'][0]['basis_type']='calculated'
  with self.assertRaises(AssertionError):self.check()
if __name__=='__main__':unittest.main()
