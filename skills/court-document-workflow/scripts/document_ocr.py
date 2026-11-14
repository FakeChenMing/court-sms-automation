"""Force OCR every document; preserve page provenance and repair failed pages."""
from pathlib import Path
import sys,json,fcntl,re,shutil,hashlib,subprocess,os
sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
import pymupdf as fitz
RUNTIME=Path(os.environ.get('COURT_RUNTIME_DIR',str(Path(__file__).resolve().parent/'runtime')))
sys.path.insert(0,str(RUNTIME))
from court_schedule_notices import run_guarded_ocr
STATE=Path(os.environ.get('COURT_STATE_DIR',str(Path.home()/'Library/Application Support/CourtSMSMonitor')))
SKILL=Path.home()/'.codex/skills/mineru-document-parsing/SKILL.md'
MINERU=Path(os.environ.get('COURT_MINERU_BIN',str(Path.home()/'.local/bin/mineru-local')))
PADDLE=Path(os.environ.get('COURT_PADDLE_BIN',str(Path.home()/'.local/bin/paddleocr-local')))

def clean(s):
 text=re.sub(r'\\([\\`*{}\[\]()#+\-.!_>~|])',r'\1',s or '')
 return re.sub(r'\s+','',re.sub(r'<[^>]*>','',text)).replace('**','')
def page_blocks(file,count):
 data=json.loads(file.read_text());pages=[[] for _ in range(count)]
 for block in data:
  i=block.get('page_idx',-1)
  if not isinstance(i,int) or not 0<=i<count:raise ValueError('OCR页码不匹配')
  for key in ('text','table_body','image_caption','table_caption','table_footnote'):
   v=block.get(key)
   if isinstance(v,list):v='\n'.join(map(str,v))
   if v:pages[i].append(str(v))
 return ['\n'.join(p) for p in pages]

def prepare(doc,allow_new=True):
 SKILL.read_text() # Required operational policy is present and loaded before OCR.
 sha=doc['sha256'];source=next((Path(p) for p in doc['paths'] if Path(p).is_file()),None)
 if not source or hashlib.sha256(source.read_bytes()).hexdigest()!=sha:raise ValueError('源文件缺失或变化')
 if source.suffix.lower()!='.pdf':raise ValueError('非PDF须转换逐页OCR，已保留待处理')
 with fitz.open(source) as pdf:count=len(pdf)
 cache=STATE/'document_ocr'/sha;cache.mkdir(parents=True,exist_ok=True)
 ready=cache/'pages.json'
 if ready.exists():
  data=json.loads(ready.read_text())
  if data['source_sha256']==sha and len(data['pages'])==count and all(len(clean(x['text']))>=25 or x.get('needs_visual_review') for x in data['pages']):return data
 files=list(cache.rglob('*content_list.json'))
 if not files:
  if not allow_new:raise RuntimeError('批量OCR处理中，等待此文书输出')
  with (STATE/'ocr_worker.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   input_path=cache/(sha+'.pdf');shutil.copy2(source,input_path)
   result=run_guarded_ocr([str(MINERU),'-p',str(input_path),'-o',str(cache/'mineru'),'-b','pipeline','-m','ocr','-f','false'],timeout=3600)
   if result.returncode:raise RuntimeError('MinerU识别失败')
  files=list(cache.rglob('*content_list.json'))
 if len(files)!=1:raise ValueError('OCR输出不唯一')
 texts=page_blocks(files[0],count);provenance=['MinerU pipeline forced OCR']*count;uncertain=[]
 for i,text in enumerate(texts):
  if len(clean(text))>=25:continue
  saved=cache/f'service-page-{i+1}.json'
  if saved.exists():
   value=json.loads(saved.read_text());answer=value.get('choices',[{}])[0].get('message',{}).get('content','')
   if 'Visible text in reading order:\n' in answer:
    repaired=answer.split('Visible text in reading order:\n',1)[1]
    if len(clean(repaired))>=25:
     texts[i]=repaired;provenance[i]='PP-OCRv6 local service targeted repair';continue
  prior=list((cache/f'repair-{i+1}').glob('*res.json'))
  if prior:
   values=[]
   for p in prior:
    value=json.loads(p.read_text());value=value.get('res',value);values.extend(value.get('rec_texts',[]))
   texts[i]='\n'.join(values);provenance[i]='PP-OCRv6 attempted; source image review required'
   if len(clean(texts[i]))<25:uncertain.append(i+1)
   continue
  if not allow_new:
   # The primary forced OCR has run on this page. Preserve its partial status;
   # do not block clear operative pages behind a seal or a picture-only appendix.
   with fitz.open(source) as pdf:native=pdf[i].get_text().strip()
   if native:texts[i]=(texts[i]+'\n[PDF文字层辅助核对，非OCR替代]\n'+native).strip()
   uncertain.append(i+1);provenance[i]='MinerU forced OCR partial; mandatory source image review'
   continue
  with (STATE/'ocr_worker.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   (Path.home()/'.codex/skills/paddleocr-text-recognition/SKILL.md').read_text()
   page_dir=cache/f'repair-{i+1}';page_dir.mkdir(exist_ok=True)
   with fitz.open(source) as pdf:pdf[i].get_pixmap(dpi=180).save(page_dir/'page.png')
   result=run_guarded_ocr([str(PADDLE),'ocr','-i',str(page_dir/'page.png'),'--save_path',str(page_dir),'--device','cpu','--ocr_version','PP-OCRv6','--use_doc_orientation_classify','False','--use_doc_unwarping','False','--use_textline_orientation','False'],timeout=1200)
   if result.returncode:raise RuntimeError('定向补识失败')
   repaired=[]
   for p in page_dir.glob('*.json'):
    x=json.loads(p.read_text());x=x.get('res',x);repaired.extend(x.get('rec_texts',[]))
   texts[i]='\n'.join(repaired);provenance[i]='PP-OCRv6 targeted page repair'
   if len(clean(texts[i]))<25:uncertain.append(i+1)
 data={'source_sha256':sha,'source_path':str(source),'page_count':count,'skill_path':str(SKILL),'uncertain_pages':uncertain,'pages':[{'page':i+1,'text':t,'engine':provenance[i],'needs_visual_review':i+1 in uncertain} for i,t in enumerate(texts)]}
 ready.write_text(json.dumps(data,ensure_ascii=False,indent=2))
 (cache/'fulltext.md').write_text('\n\n'.join(f'## 第{p["page"]}页\n{p["text"]}' for p in data['pages']))
 data['text_path']=str(cache/'fulltext.md');ready.write_text(json.dumps(data,ensure_ascii=False,indent=2));return data
