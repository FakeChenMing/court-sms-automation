import json,tempfile,unittest,sys,asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import workflow_worker as w
class ModelHealth(unittest.TestCase):
 def test_new_provider_does_not_inherit_old_402_cooldown(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)):
   status=Path(tmp)/'model_service.json';status.write_text(json.dumps({'available':False,'code':'MODEL_HTTP_402','retry_at':'9999'}))
   response=SimpleNamespace(returncode=0,stdout=json.dumps({'type':'message','content':[{'type':'text','text':'{"ok":true}'}]}))
   with patch.object(w,'run_model_bridge',return_value=response) as call:w.model({'messages':[],'output_config':{'effort':'medium'}})
   sent=json.loads(call.call_args.kwargs['input'])
   self.assertEqual(sent['output_config']['effort'],'max')
   self.assertEqual(call.call_args.args[0][1],str(w.HERE/'gpt_bridge.cjs'))
   self.assertNotIn('DeepSeek',' '.join(call.call_args.args[0]))
   saved=json.loads(status.read_text());self.assertEqual(saved['service'],'codex-cli');self.assertEqual(saved['model'],'gpt-6.1-sol');self.assertEqual(saved['reasoning_effort'],'max')
 def test_gpt_quota_is_recorded_and_held_without_provider_fallback(self):
  diagnostic={'schema_version':1,'service':'codex-cli','model':'gpt-6.1-sol','code':'MODEL_GPT_QUOTA','reason':'quota_exceeded'}
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)),patch.object(w,'run_model_bridge',return_value=SimpleNamespace(returncode=1,stderr='MODEL_GPT_QUOTA\nMODEL_DIAGNOSTIC '+json.dumps(diagnostic))) as call:
   with self.assertRaisesRegex(RuntimeError,'额度已用尽'):w.model({'messages':[]})
   with self.assertRaisesRegex(RuntimeError,'额度已用尽'):w.model({'messages':[]})
   self.assertEqual(call.call_count,1)
 def test_transport_timeout_is_recorded_without_raw_input(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)),patch.object(w,'run_model_bridge',side_effect=w.subprocess.TimeoutExpired('private-command',660,output='private-input')):
   with self.assertRaisesRegex(RuntimeError,'MODEL_GPT_TIMEOUT'):w.model({'messages':[]})
   saved=(Path(tmp)/'model_service.json').read_text();self.assertIn('MODEL_GPT_TIMEOUT',saved);self.assertNotIn('private',saved)
 def test_bad_response_does_not_mark_old_failure_recovered(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)):
   state=Path(tmp)/'model_service.json';state.write_text(json.dumps({'available':False,'code':'MODEL_HTTP_402'}))
   with patch.object(w,'run_model_bridge',return_value=SimpleNamespace(returncode=0,stdout='{}')):
    with self.assertRaisesRegex(RuntimeError,'有效结构化'):w.model({'messages':[]},bypass_cooldown=True)
   self.assertFalse(json.loads(state.read_text())['available'])
 def diagnostic(self,**updates):
  value={'schema_version':1,'service':'deepseek-account','model':'deepseek-v4-pro','code':'MODEL_HTTP_402','http_status':402,'reason':'insufficient_balance','request_id':'00000000-0000-4000-8000-000000000001'}
  value.update(updates)
  return 'MODEL_HTTP_402\nMODEL_DIAGNOSTIC '+json.dumps(value)
 def test_safe_balance_reason_survives_cooldown_without_raw_text(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)),patch.object(w,'run_model_bridge',return_value=SimpleNamespace(returncode=1,stderr=self.diagnostic(message='secret-token-and-case-content',unexpected='secret-token-and-case-content'))) as call:
   with self.assertRaisesRegex(RuntimeError,'账户余额不足'):w.model({'messages':[]})
   with self.assertRaisesRegex(RuntimeError,'账户余额不足'):w.model({'messages':[]})
   state_text=(Path(tmp)/'model_service.json').read_text();state=json.loads(state_text)
   self.assertEqual(call.call_count,1)
   self.assertEqual(state['diagnostic']['request_id'],'00000000-0000-4000-8000-000000000001')
   self.assertNotIn('secret-token',state_text)
 def test_unknown_error_is_not_inferred_as_insufficient_balance(self):
  data=w.model_diagnostic(self.diagnostic(reason='arbitrary-provider-error',request_id='secret-token'),'MODEL_HTTP_402')
  self.assertEqual(data['reason'],'unclassified');self.assertNotIn('request_id',data)
  self.assertEqual(w.model_diagnostic('MODEL_HTTP_402','MODEL_HTTP_402'),{})
 def test_mismatched_diagnostic_is_ignored(self):
  self.assertEqual(w.model_diagnostic(self.diagnostic(http_status=401),'MODEL_HTTP_402'),{})
  self.assertEqual(w.model_diagnostic(self.diagnostic(service='unknown-provider'),'MODEL_HTTP_402'),{})
 def test_successful_probe_clears_old_failure_diagnostic(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)):
   state=Path(tmp)/'model_service.json';state.write_text(json.dumps({'available':False,'retry_at':'9999','diagnostic':{'reason':'insufficient_balance'}}))
   with patch.object(w,'run_model_bridge',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'type':'message','content':[{'type':'text','text':'{"ok":true}'}]}))):
    w.model({'messages':[]},bypass_cooldown=True)
   self.assertTrue(json.loads(state.read_text())['available']);self.assertNotIn('diagnostic',json.loads(state.read_text()))
 def test_402_is_reported_and_repeated_calls_are_held(self):
  with tempfile.TemporaryDirectory() as tmp, patch.object(w,'STATE',Path(tmp)), patch.object(w,'run_model_bridge',return_value=SimpleNamespace(returncode=1,stderr='MODEL_HTTP_402')) as call:
   with self.assertRaisesRegex(RuntimeError,'402'):w.model({'messages':[]})
   with self.assertRaisesRegex(RuntimeError,'402'):w.model({'messages':[]})
   self.assertEqual(call.call_count,1)
   state=json.loads((Path(tmp)/'model_service.json').read_text());self.assertFalse(state['available']);self.assertIn('retry_at',state)
 def test_health_distinguishes_running_from_complete(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)):
   data={'documents':{'a':{'id':'a','paths':['/source.pdf'],'case_number':'case','review_status':'attention','ocr_status':'complete','decision':{'unresolved':['anchor missing']}}},'events':{}}
   w.write_health(data,0,{'available':False,'code':'MODEL_HTTP_402'})
   health=json.loads((Path(tmp)/'workflow_health.json').read_text());self.assertEqual(health['status'],'model_unavailable');self.assertEqual(health['pending_check_documents'],1);self.assertEqual(health['calendar_receipts'],0)
 def test_health_does_not_count_merged_aliases_as_active_calendar_receipts(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'STATE',Path(tmp)):
   data={'documents':{},'events':{
    'old-reused':{'uid':'same-uid','merged_into':'group','sync_pending':True},
    'old-deleted':{'uid':'removed-uid','calendar_removed_by_receipt_merge':True},
    'group':{'uid':'same-uid','verified':True},
    'deadline':{'uid':'deadline-uid','verified':True,'sync_pending':True}}}
   w.write_health(data,0,{'available':True})
   health=json.loads((Path(tmp)/'workflow_health.json').read_text())
   self.assertEqual(health['calendar_receipts'],2)
   self.assertEqual(health['archived_receipt_aliases'],2)
   self.assertEqual(health['attachment_sync_pending'],1)
if __name__=='__main__':unittest.main()
