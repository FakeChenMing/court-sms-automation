"""Read-only legal MCP transport, independent of the model-provider application.

Deployments may set COURT_LEGAL_MCP_NODE in launchd EnvironmentVariables to an
absolute native Node executable.  Otherwise use the bundled stable runtime;
never search PATH or fall back to a model-provider application executable.
"""
import asyncio,json,sys,subprocess,os
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
try:import tomllib
except ImportError:import tomli as tomllib
ALLOWED=('mcp-law','yuandian-law')
DEFAULT_NODE=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
def node_runtime():
 configured=os.environ.get('COURT_LEGAL_MCP_NODE')
 node=Path(configured) if configured is not None else DEFAULT_NODE
 if not node.is_absolute() or not node.is_file() or not os.access(node,os.X_OK):
  raise RuntimeError('法律MCP Node运行环境不可用：需配置可执行文件的绝对路径')
 return str(node.resolve())
def isolated(payload):
 label=payload['server'];assert label in ALLOWED
 c=tomllib.loads(Path(os.environ.get('COURT_CODEX_CONFIG',str(Path.home()/'.codex/config.toml'))).read_text())['mcp_servers'][label]
 request={'url':c['url'],'headers':c.get('http_headers',{}),'method':'tools/list' if payload['operation']=='list' else 'tools/call','arguments':{} if payload['operation']=='list' else {'name':payload['name'],'arguments':payload['arguments']}}
 node=node_runtime()
 env=os.environ.copy()
 for name in ('ELECTRON_RUN_AS_NODE','NODE_OPTIONS','NODE_PATH'):env.pop(name,None)
 for attempt in range(3):
  try:
   p=subprocess.run([node,str(Path(__file__).with_name('mcp_bridge.cjs'))],input=json.dumps(request),capture_output=True,text=True,timeout=240,env=env)
   x=json.loads(p.stdout)
   if not p.returncode and 'transport_error' not in x:return x
  except (subprocess.TimeoutExpired,json.JSONDecodeError):pass
 raise RuntimeError('法律MCP连接失败')
class LegalSources:
 def __init__(self):self.sessions={};self.tools={};self.errors={};self.audit=[]
 async def __aenter__(self):
  for label in ALLOWED:
   try:
    result=await asyncio.to_thread(isolated,{'server':label,'operation':'list'});self.sessions[label]=True
    for t in result['tools']:
     if not any(s in t['name'].lower() for s in ('search','get','detail')) or any(s in t['name'].lower() for s in ('balance','policy','user')):continue
     name=label.replace('-','_')+'__'+t['name'];self.tools[name]={'name':name,'description':t.get('description',''),'input_schema':t['inputSchema'],'server':label,'remote_name':t['name']}
   except Exception as e:self.errors[label]=type(e).__name__
  return self
 async def __aexit__(self,*args):return False
 def schemas(self):return [{k:v for k,v in x.items() if k in ('name','description','input_schema')} for x in self.tools.values()]
 async def call(self,name,args):
  if name not in self.tools:raise ValueError('未授权法律工具')
  tool=self.tools[name]
  try:result=await asyncio.to_thread(isolated,{'server':tool['server'],'operation':'call','name':tool['remote_name'],'arguments':args})
  except Exception as e:result={'isError':True,'content':[{'type':'text','text':'法律MCP请求失败：'+type(e).__name__}]}
  record={'id':f'legal-{len(self.audit)+1}','server':tool['server'],'tool':tool['remote_name'],'arguments':args,'queried_at':datetime.now().astimezone().isoformat(),'isError':bool(result.get('isError')),'content':[x for x in result.get('content',[]) if x.get('type')=='text']}
  self.audit.append(record);return record
async def smoke():
 async with LegalSources() as s:print(json.dumps({'servers':list(s.sessions),'tool_names':list(s.tools),'errors':s.errors}))
if __name__=='__main__':asyncio.run(smoke())
