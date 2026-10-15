#!/usr/bin/env python3
"""Explicit prepare/activate/stop/doctor commands. No automatic installation on import."""
import argparse,datetime,json,os,pathlib,plistlib,shutil,sqlite3,subprocess,sys,hashlib
SOURCE=pathlib.Path(__file__).resolve().parent/'runtime'
LABEL='local.court-sms-automation'
def atomic(path,data):
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.chmod(0o600);tmp.replace(path)
def config_at(target):
 return json.loads((target/'runtime/config.json').read_text())
def build_app(runtime,name,exe,source,kind):
 app=runtime/(name+'.app');contents=app/'Contents';binary=contents/'MacOS'/exe;binary.parent.mkdir(parents=True,exist_ok=True)
 subprocess.run(['xcrun','clang','-fobjc-arc','-framework','Foundation','-framework','EventKit',str(runtime/source),'-o',str(binary)],check=True)
 info={'CFBundleExecutable':exe,'CFBundleIdentifier':'local.court-sms-automation.'+exe.replace('_','-'),'CFBundleName':name,'CFBundlePackageType':'APPL','CFBundleVersion':'1','LSUIElement':True,kind:'用于用户授权的法院短信材料提醒或日历记录。'}
 (contents/'Info.plist').write_bytes(plistlib.dumps(info));subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
def prepare(a):
 target=a.target.resolve();runtime=target/'runtime';state=target/'state'
 if target.exists() and any(target.iterdir()):raise RuntimeError('Target is not empty. Existing installations require a reviewed upgrade; no overwrite.')
 if sys.platform!='darwin':raise RuntimeError('macOS required')
 if sys.version_info<(3,9):raise RuntimeError('Python 3.9+ required')
 if int(subprocess.check_output(['sw_vers','-productVersion'],text=True).split('.')[0])<14:raise RuntimeError('macOS 14+ required')
 datetime.date.fromisoformat(a.start_date)
 output=a.output.expanduser().resolve()
 # Copies only distributable source and vendor code. No live state, Messages DB or credentials.
 shutil.copytree(SOURCE,runtime,ignore=shutil.ignore_patterns('__pycache__','*.pyc'));state.mkdir(mode=0o700)
 cfg={'start_date':a.start_date,'output_dir':str(output),'state_dir':str(state),'chat_db':str(pathlib.Path.home()/'Library/Messages/chat.db'),'reminder_list':a.reminder_list,'calendar_enabled':bool(a.calendar),'calendar_name':a.calendar or '', 'mineru_bin':a.mineru or ''}
 atomic(runtime/'config.json',cfg)
 initial={'last_rowid':0,'processed_rowids':[],'downloaded_urls':[],'pending_reports':[],'pending_summons':[],'pending_reminders':[],'manual_review':[],'message_jobs':{},'last_updated':datetime.datetime.now().isoformat(timespec='seconds')}
 atomic(state/'state.json',initial);atomic(state/'state.previous.json',initial)
 build_app(runtime,'CourtReminderAgent','reminder_bridge','reminder_bridge.m','NSRemindersFullAccessUsageDescription')
 if a.calendar:build_app(runtime,'CourtEventAttachment','court_event_attachment','calendar_bridge.m','NSCalendarsFullAccessUsageDescription')
 plist={'Label':LABEL,'ProgramArguments':[str(pathlib.Path(sys.executable).resolve()),str(runtime/'court_sms_monitor.py')],'RunAtLoad':True,'StartInterval':60,'ProcessType':'Background','WorkingDirectory':str(runtime),'StandardOutPath':str(state/'stdout.log'),'StandardErrorPath':str(state/'stderr.log'),'EnvironmentVariables':{'HOME':str(pathlib.Path.home()),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'zh_CN.UTF-8'}}
 (target/(LABEL+'.plist')).write_bytes(plistlib.dumps(plist));print(json.dumps({'prepared':str(target),'activated':False,'next':'Review config, grant permissions, run doctor, then explicitly activate.'}))
def doctor(a):
 cfg=config_at(a.target);state=pathlib.Path(cfg['state_dir']);s=json.loads((state/'state.json').read_text());out={'last_rowid':s.get('last_rowid'),'last_scan_at':s.get('last_scan_at'),'queues':{k:len(s.get(k,[])) for k in ['pending_reports','pending_summons','pending_reminders','manual_review']},'jobs':{},'last_report_success_at':s.get('last_report_success_at')}
 for j in s.get('message_jobs',{}).values():out['jobs'][j['status']]=out['jobs'].get(j['status'],0)+1
 try:
  with sqlite3.connect('file:'+cfg['chat_db']+'?mode=ro',uri=True) as db:
   cut=int((datetime.datetime.fromisoformat(cfg['start_date']).timestamp()-978307200)*1e9)
   out['max_in_scope_incoming']=db.execute('select max(rowid) from message where is_from_me=0 and date>=?',(cut,)).fetchone()[0]
 except (OSError,sqlite3.Error) as e:out['sms_db_error']=type(e).__name__
 out['hash_checked']=0;out['hash_failures']=0
 manifests=sorted(pathlib.Path(cfg['output_dir']).glob('*/**/.delivery_manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]
 for p in manifests:
  for doc in json.loads(p.read_text()).get('documents',[]):
   f=pathlib.Path(doc['path']);out['hash_checked']+=1
   if not f.is_file() or f.stat().st_size!=doc['size'] or hashlib.sha256(f.read_bytes()).hexdigest()!=doc['sha256']:out['hash_failures']+=1
 print(json.dumps(out,ensure_ascii=False,indent=2))
def activate(a):
 config_at(a.target);dest=pathlib.Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
 if dest.exists():raise RuntimeError('LaunchAgent already exists; do not overwrite another installation')
 dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(a.target/(LABEL+'.plist'),dest)
 subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(dest)],check=True);print('activated')
def stop(a):
 cfg=config_at(a.target);dest=pathlib.Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
 if not dest.exists():raise RuntimeError('No installed agent')
 p=plistlib.loads(dest.read_bytes())
 if p.get('ProgramArguments',[None,None])[1]!=str(a.target/'runtime/court_sms_monitor.py'):raise RuntimeError('Agent belongs to a different target')
 subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],check=True)
 dest.rename(dest.with_suffix('.plist.disabled'));print('stopped; source, state and materials preserved')
def backfill(a):
 import importlib.util,fcntl
 cfg=config_at(a.target)
 begin=datetime.datetime.fromisoformat(a.since);end=datetime.datetime.fromisoformat(a.until)+datetime.timedelta(days=1)
 if begin<datetime.datetime.fromisoformat(cfg['start_date']) or end<=begin:raise RuntimeError('Range outside configured scope')
 spec=importlib.util.spec_from_file_location('court_runtime',a.target/'runtime/court_sms_monitor.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 with sqlite3.connect('file:'+cfg['chat_db']+'?mode=ro',uri=True) as db:
  rows=db.execute('select rowid,text,attributedBody,date from message where is_from_me=0 and date>=? and date<? order by rowid',(int((begin.timestamp()-978307200)*1e9),int((end.timestamp()-978307200)*1e9))).fetchall()
 selected=[]
 for rid,text,body,date in rows:
  text=m.decode_message_text(text,body)
  if m.is_court_message(text,m.extract_urls(text)):selected.append({'rowid':rid,'text':text,'date':date})
 if a.apply:
  with m.LOCK_PATH.open('a+') as lock:
   fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);state=m.load_state()
   for msg in selected:state['message_jobs'][str(msg['rowid'])]={'message':msg,'status':'pending','attempts':0,'received_at':m.message_received_at(msg['date']).isoformat(),'next_retry_at':''}
   m.save_state(state)
 print(json.dumps({'matched':len(selected),'rowids':[v['rowid'] for v in selected],'applied':a.apply}))
def main():
 p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
 for command in ['prepare','doctor','activate','stop','backfill']:
  c=sub.add_parser(command);c.add_argument('--target',type=pathlib.Path,default=pathlib.Path.home()/'Library/Application Support/CourtSMSAutomation')
  if command=='backfill':
   c.add_argument('--since',required=True);c.add_argument('--until',required=True);c.add_argument('--apply',action='store_true')
  if command=='prepare':
   c.add_argument('--start-date',required=True);c.add_argument('--output',type=pathlib.Path,required=True);c.add_argument('--reminder-list',required=True);c.add_argument('--calendar');c.add_argument('--mineru')
 a=p.parse_args();a.target=a.target.expanduser().resolve();globals()[a.command](a)
if __name__=='__main__':main()
