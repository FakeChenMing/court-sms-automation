"""Second OCR engine for source checks; never pretend a text model can see images."""
import json,base64,hashlib,fcntl,time
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request
from document_ocr import STATE,clean,fitz
from court_schedule_notices import memory_fraction

def read_page(document,page):
 source=Path(document['source_path']);sha=document['source_sha256']
 if hashlib.sha256(source.read_bytes()).hexdigest()!=sha:raise ValueError('核对时原件已变化')
 cache=STATE/'document_ocr'/sha;cache.mkdir(parents=True,exist_ok=True)
 manual=cache/f'manual-page-{page}.json'
 if manual.exists():
  attestation=json.loads(manual.read_text())
  if attestation.get('source_sha256')!=sha or attestation.get('page')!=page or attestation.get('reviewed') is not True:raise ValueError('原图核对记录不匹配')
  return {'doc_id':document['id'],'page':page,'engine':'direct source-image review','text':'','quality':'实际原图核对记录；未宣称第二OCR完整','manual_source_review':attestation,'readable_text':False}
 saved=cache/f'service-page-{page}.json'
 if not saved.exists():
  (Path.home()/'.codex/skills/paddleocr-text-recognition/SKILL.md').read_text()
  with (STATE/'ocr_worker.lock').open('a+') as lock:
   deadline=time.monotonic()+1200
   while True:
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
    except BlockingIOError:
     if time.monotonic()>=deadline:raise RuntimeError('另一OCR任务尚未完成，保留核对待办')
     time.sleep(.25)
   if memory_fraction()>=.80:raise RuntimeError('内存超过80%，暂缓第二引擎核对')
   with fitz.open(source) as pdf:raw=pdf[page-1].get_pixmap(dpi=160).tobytes('jpeg',jpg_quality=90)
   body={'model':'paddleocr-local','messages':[{'role':'user','content':[{'type':'text','text':'逐行完整识别文字，不省略日期、姓名、地点。'},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(raw).decode()}}]}]}
   req=Request('http://127.0.0.1:11435/v1/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
   with build_opener(ProxyHandler({})).open(req,timeout=180) as response:data=json.load(response)
   tmp=saved.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False));tmp.replace(saved)
 data=json.loads(saved.read_text());answer=data.get('choices',[{}])[0].get('message',{}).get('content','')
 text=answer.split('Visible text in reading order:\n',1)[1] if 'Visible text in reading order:\n' in answer else ''
 attestation=None;manual=cache/f'manual-page-{page}.json'
 if manual.exists():
  attestation=json.loads(manual.read_text())
  if attestation.get('source_sha256')!=sha or attestation.get('page')!=page or attestation.get('reviewed') is not True:raise ValueError('原图核对记录不匹配')
 return {'doc_id':document['id'],'page':page,'engine':'local PP-OCRv6','text':text,'quality':answer.split('Visible text in reading order:\n',1)[0],'manual_source_review':attestation,'readable_text':len(clean(text))>=25}
