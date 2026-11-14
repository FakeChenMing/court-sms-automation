import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import legal_sources as legal


class LegalNodeRuntime(unittest.TestCase):
 def test_transport_uses_absolute_node_and_preserves_bounded_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'.codex').mkdir()
   (root/'.codex/config.toml').write_text('[mcp_servers.mcp-law]\nurl="https://apim-gateway.pkulaw.com/test"\n')
   node=root/'node';node.write_text('test fixture');node.chmod(0o700)
   failed=SimpleNamespace(returncode=1,stdout='{"transport_error":"HTTP_503"}')
   success=SimpleNamespace(returncode=0,stdout='{"tools":[]}')
   with patch.object(legal.Path,'home',return_value=root),patch.dict(os.environ,{'COURT_LEGAL_MCP_NODE':str(node),'PATH':'/untrusted','NODE_OPTIONS':'--require /untrusted/preload','NODE_PATH':'/untrusted','ELECTRON_RUN_AS_NODE':'1'},clear=True),patch.object(legal.subprocess,'run',side_effect=[failed,success]) as run:
    self.assertEqual(legal.isolated({'server':'mcp-law','operation':'list'}),{'tools':[]})
    self.assertEqual(run.call_count,2)
    for call in run.call_args_list:
     self.assertEqual(call.args[0][0],str(node.resolve()))
     self.assertEqual(call.kwargs['timeout'],240)
     self.assertEqual(json.loads(call.kwargs['input'])['method'],'tools/list')
     self.assertNotIn('DeepSeek',' '.join(call.args[0]))
     for key in ('NODE_OPTIONS','NODE_PATH','ELECTRON_RUN_AS_NODE'):self.assertNotIn(key,call.kwargs['env'])

 def test_missing_or_relative_config_never_searches_path_or_falls_back(self):
  for value in ('node','/missing-court-test/node',''):
   with self.subTest(value=value),patch.dict(os.environ,{'COURT_LEGAL_MCP_NODE':value},clear=True),patch.object(legal.subprocess,'run') as run:
    with self.assertRaisesRegex(RuntimeError,'绝对路径'):legal.node_runtime()
    run.assert_not_called()

 def test_default_is_bundled_native_node_and_timeouts_remain_three_attempts(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'.codex').mkdir()
   (root/'.codex/config.toml').write_text('[mcp_servers.yuandian-law]\nurl="https://open.chineselaw.com/test"\n')
   node=root/'native-node';node.write_text('test fixture');node.chmod(0o700)
   with patch.object(legal.Path,'home',return_value=root),patch.object(legal,'DEFAULT_NODE',node),patch.dict(os.environ,{'PATH':'/untrusted'},clear=True),patch.object(legal.subprocess,'run',side_effect=subprocess.TimeoutExpired('native-node',240)) as run:
    with self.assertRaisesRegex(RuntimeError,'法律MCP连接失败'):legal.isolated({'server':'yuandian-law','operation':'list'})
    self.assertEqual(run.call_count,3)
    self.assertTrue(all(call.args[0][0]==str(node.resolve()) for call in run.call_args_list))


if __name__=='__main__':unittest.main()
