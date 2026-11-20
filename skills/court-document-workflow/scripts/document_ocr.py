"""Extract text or OCR each page; preserve page provenance and repair failed pages."""
from pathlib import Path
import sys,json,fcntl,re,shutil,hashlib,subprocess,os
sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
import pymupdf as fitz
RUNTIME=Path(os.environ.get('COURT_RUNTIME_DIR',str(Path(__file__).resolve().parent/'runtime')))
sys.path.insert(0,str(RUNTIME))
from quark_ocr import read, save
import ocr_markdown as md
STATE=Path(os.environ.get('COURT_STATE_DIR',str(Path.home()/'Library/Application Support/CourtSMSMonitor')))
SKILL=Path.home()/'.codex/skills/mineru-document-parsing/SKILL.md'

def clean(s):
 text=re.sub(r'\\([\\`*{}\[\]()#+\-.!_>~|])',r'\1',s or '')
 return re.sub(r'\s+','',re.sub(r'<[^>]*>','',text)).replace('**','')
def prepare(doc,allow_new=True):
 sha=doc['sha256']
 source=next((Path(p) for p in doc['paths'] if Path(p).is_file() and md.fingerprint(Path(p))==sha),None)
 if not source:raise ValueError('源文件缺失或变化')
 cache=STATE/'document_ocr'/sha
 cache.mkdir(parents=True,exist_ok=True)
 ready=cache/'pages.json'
 with (cache/'prepare.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if ready.exists():
   data=json.loads(ready.read_text())
   if data.get('source_sha256')==sha and data.get('pages') and Path(data.get('text_path') or cache/'fulltext.md').is_file():
    with fitz.open(source) as original: count=len(original)
    if len(data['pages'])==count and all(p.get('page')==i and 'text' in p for i,p in enumerate(data['pages'],1)):
     data['source_path']=str(source)
     data['text_path']=str(cache/'fulltext.md')
     with md.writing(source):
      path=md.target(source)
      md.read_companion(path,source,sha,'auto')
      md.publish(path,source,sha,'auto',data['pages'])
     data['markdown_path']=str(path)
     return data
  pages=read(source,allow_cloud=allow_new)
  if not pages or any(p['source_sha256']!=sha for p in pages):raise ValueError('页来源不一致')
  text_path=cache/'fulltext.md'
  data=dict(source_sha256=sha,source_path=str(source),page_count=len(pages),skill_path=str(SKILL),
            pages=pages,uncertain_pages=[p['page'] for p in pages if p['needs_visual_review']],text_path=str(text_path))
  data['markdown_path']=str(md.target(source))
  save(text_path,'\n\n'.join(f"## 第{p['page']}页\n{p['text']}" for p in pages))
  save(ready,json.dumps(data,ensure_ascii=False,indent=2))
  return data
