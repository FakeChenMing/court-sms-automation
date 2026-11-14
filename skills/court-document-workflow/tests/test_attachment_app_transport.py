"""Normal app launch transport; simulated open only, no real Calendar access."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'scripts/runtime/court_document_review.py'
spec=importlib.util.spec_from_file_location('attachment_app_transport_test_module',SOURCE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class AttachmentAppTransport(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.app=self.root/'CourtEventAttachment.app'
  self.helper=self.app/'Contents/MacOS/court_event_attachment';self.helper.parent.mkdir(parents=True);self.helper.write_text('fixture')
  self.path=self.root/'plan.json';self.plan={'uid':'known-uid','event_key':'known-key','attachment_manifest':[{'source_path':'/fixture.pdf','sha256':'a'*64}],'alarm_minutes':-10080}
  self.path.write_text(json.dumps(self.plan))
  self.value={'uid':'known-uid','event_key':'known-key','extras_verified':True,'metadata_preserved':True,
              'attachments':[{'source_path':'/fixture.pdf','sha256':'a'*64,'content_verified':True}],
              'alarms':[{'relative_minutes':-10080,'absolute_timestamp':None}]}
  self.runtime=patch.multiple(m,ATTACHMENT_HELPER=self.helper,STATE_DIR=self.root);self.runtime.start();self.addCleanup(self.runtime.stop)
 def fake_open(self,stdout=None,stderr='',code=0,open_stderr=''):
  def run(command,**kwargs):
   self.command=command
   if stdout is not None:Path(command[command.index('--stdout')+1]).write_text(stdout)
   Path(command[command.index('--stderr')+1]).write_text(stderr)
   return subprocess.CompletedProcess(command,code,'',open_stderr)
  return run
 def test_valid_receipt_uses_normal_app_identity_and_checks_content(self):
  with patch('subprocess.run',side_effect=self.fake_open(json.dumps(self.value))):result=m.run_attachment_helper('review-inspect',self.path)
  self.assertEqual(result.returncode,0)
  self.assertEqual(self.command[:3],['/usr/bin/open','-n','-W'])
  self.assertIn(str(self.app),self.command);self.assertNotIn(str(self.helper),self.command)
  self.assertEqual(self.command[-3:],['--args','review-inspect',str(self.path.resolve())])
 def test_open_zero_without_valid_json_is_failure(self):
  for output in (None,'','not JSON','{}'):
   with self.subTest(output=output),patch('subprocess.run',side_effect=self.fake_open(output)):
    self.assertNotEqual(m.run_attachment_helper('review-inspect',self.path).returncode,0)
 def test_error_stderr_blocks_even_valid_json_and_preserves_failure_class(self):
  with patch('subprocess.run',side_effect=self.fake_open(json.dumps(self.value),'日历附件权限不可用')):
   result=m.run_attachment_helper('review-inspect',self.path)
  self.assertEqual(result.returncode,77);self.assertIn('权限',result.stderr)
  diagnostic=json.loads(Path(result.transport_diagnostic_path).read_text())
  self.assertEqual(diagnostic['reason'],'helper_stderr');self.assertEqual(diagnostic['result_code'],77)
 def test_exact_wait_race_passes_only_with_valid_full_receipt(self):
  race='Unable to block on application (GetProcessPID() returned 18446744073709551016)'
  with patch('subprocess.run',side_effect=self.fake_open(json.dumps(self.value),code=1,open_stderr=race)):
   result=m.run_attachment_helper('review-inspect',self.path)
  self.assertEqual(result.returncode,0)
  self.assertEqual(result.transport_warning,'launchservices_wait_process_race')
  self.assertEqual(json.loads(result.stdout)['transport_warning'],result.transport_warning)
  self.assertEqual(json.loads(Path(result.transport_diagnostic_path).read_text())['reason'],result.transport_warning)
 def test_wait_race_without_receipt_or_with_bad_identity_sha_alarm_still_fails(self):
  race='Unable to block on application (GetProcessPID() returned 18446744073709551016)'
  variants=[None,'','not JSON','{}']
  for field in ('uid','event_key'):
   value=json.loads(json.dumps(self.value));value[field]='wrong';variants.append(json.dumps(value))
  value=json.loads(json.dumps(self.value));value['attachments'][0]['sha256']='wrong';variants.append(json.dumps(value))
  value=json.loads(json.dumps(self.value));value['alarms']=[];variants.append(json.dumps(value))
  for output in variants:
   with self.subTest(output=output),patch('subprocess.run',side_effect=self.fake_open(output,code=1,open_stderr=race)):
    result=m.run_attachment_helper('review-inspect',self.path)
   self.assertNotEqual(result.returncode,0)
   self.assertEqual(json.loads(Path(result.transport_diagnostic_path).read_text())['reason'],'wait_race_invalid_receipt')
 def test_other_open_errors_or_helper_stderr_are_never_ignored(self):
  race='Unable to block on application (GetProcessPID() returned 18446744073709551016)'
  for open_error,helper_error in ((race+'\nAnother launch error',''),(race.replace('51016','51017'),''),('Another launch error',''),(race,'日历附件权限不可用')):
   with self.subTest(open_error=open_error,helper_error=helper_error),patch('subprocess.run',side_effect=self.fake_open(json.dumps(self.value),helper_error,1,open_error)):
    result=m.run_attachment_helper('review-sync',self.path)
   self.assertNotEqual(result.returncode,0);self.assertFalse(hasattr(result,'transport_warning'))
 def test_identity_or_sha_or_alarm_mismatch_never_passes(self):
  variants=[]
  for field in ('uid','event_key','extras_verified','metadata_preserved'):
   value=json.loads(json.dumps(self.value));value[field]='wrong' if field in ('uid','event_key') else False;variants.append(value)
  value=json.loads(json.dumps(self.value));value['attachments'][0]['sha256']='wrong';variants.append(value)
  value=json.loads(json.dumps(self.value));value['alarms']=[];variants.append(value)
  for value in variants:
   with self.subTest(value=value),patch('subprocess.run',side_effect=self.fake_open(json.dumps(value))):
    self.assertNotEqual(m.run_attachment_helper('review-sync',self.path).returncode,0)


if __name__=='__main__':unittest.main()
