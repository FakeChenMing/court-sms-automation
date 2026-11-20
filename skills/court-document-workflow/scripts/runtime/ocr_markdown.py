"""Publish and reuse one review-aware Markdown companion per source document."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(os.environ.get('QUARK_OCR_DIR', Path.home()/'Library/Application Support/QuarkOCR'))
COURT = Path(os.environ.get('COURT_STATE_DIR', Path.home()/'Library/Application Support/CourtSMSMonitor'))/'document_ocr'
FORMAT = '<!-- ocr-markdown-v1 '
PAGE = '<!-- ocr-page '
END = ' -->'

def fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def packed(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e')

def marker(line, prefix):
    if not line.startswith(prefix) or not line.endswith(END):
        return {}
    return json.loads(line[len(prefix):-len(END)])

def atomic(path, text):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', encoding='utf-8', delete=False) as stream:
        stream.write(text)
    os.replace(stream.name,path)

def lock_file(stream):
    if os.name == 'nt':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX)

@contextmanager
def writing(source):
    locks=ROOT/'markdown-locks'
    locks.mkdir(parents=True,exist_ok=True)
    identity=str(source.parent.resolve()/source.stem).casefold()
    with (locks/hashlib.sha256(identity.encode()).hexdigest()).open('a') as lock:
        lock_file(lock)
        yield

def target(source):
    directory=source.parent/'OCR文本'
    for name in (source.stem+'.md',source.name+'.md'):
        path=directory/name
        if not path.exists():return path
        with path.open(encoding='utf-8') as stream:
            if marker(stream.readline().rstrip('\n'),FORMAT).get('source')==source.name:return path
    raise FileExistsError('已有同名文字稿，无法安全确定归属')

def cached_court(sha):
    path=COURT/sha/'pages.json'
    if path.is_file():
        data=json.loads(path.read_text())
        rows=data.get('pages',[])
        if (data.get('source_sha256')==sha and rows and len(rows)==data.get('page_count')
            and all(p.get('page')==n and isinstance(p.get('text'),str) for n,p in enumerate(rows,1))):
            return [dict(p,source_sha256=sha) for p in rows]
    return None

def saved_review(sha,index):
    for root in (COURT,ROOT/'reviews'):
        path=root/sha/f'manual-page-{index}.json'
        if path.exists():
            record=json.loads(path.read_text())
            if record.get('source_sha256')==sha and record.get('page')==index and record.get('reviewed') is True:
                return record
    return {}

def record_review(source,record):
    sha=fingerprint(source)
    index=record.get('page')
    if (record.get('source_sha256')!=sha or record.get('reviewed') is not True
        or type(index) is not int or index<1 or not str(record.get('findings','')).strip()):
        raise ValueError('复核记录须与原件指纹、页码匹配并提供原图所见')
    import pymupdf as fitz
    with fitz.open(source) as doc:
        if index>len(doc):raise ValueError('复核页码超出原件')
    directory=ROOT/'reviews'/sha
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic(directory/f'manual-page-{index}.json',json.dumps(record,ensure_ascii=False))

def read_companion(path,source,sha,kind):
    if not path.exists():return None
    first,_,body=path.read_text(encoding='utf-8').partition('\n')
    info=marker(first,FORMAT)
    if info.get('source')!=source.name:return None
    if hashlib.sha256(body.encode()).hexdigest()!=info.get('body_sha256'):
        raise ValueError(f'文字稿已有额外编辑，已保留，请核对：{path}')
    if info.get('sha256')!=sha or kind not in ('auto',info.get('kind')):return None
    pages=[]
    pos=0
    while len(pages)<info['pages']:
        loc=body.index('\n'+PAGE,pos)+1
        end=body.index('\n',loc)
        meta=marker(body[loc:end],PAGE)
        size=meta.pop('length')
        meta['text']=body[end+1:end+1+size]
        if meta['page']!=len(pages)+1 or meta['source_sha256']!=sha:raise ValueError('文字稿页码或指纹不符')
        pages.append(meta)
        pos=end+1+size
    return pages

def publish(path,source,sha,kind,pages):
    parts=[f'# {source.name}\n\n原件 SHA-256：`{sha}`\n\n共 {len(pages)} 页。\n']
    for i,p in enumerate(pages,1):
        if p.get('page')!=i or p.get('source_sha256',sha)!=sha:raise ValueError('页码或原件指纹不符')
        record=saved_review(sha,i)
        state='已核对原图' if record else ('待核对原图' if p.get('needs_visual_review') else '未作原图复核（本页未标记异常）')
        parts.append(f'\n## 第 {i} 页\n\n原件 SHA-256：`{sha}`  \n识别方式：{p.get("engine","既有识别结果")}  \n原图复核：**{state}**\n')
        for k,label in [('findings','复核记录'),('transcription','原图核对摘录')]:
            if record.get(k):parts.append(f'\n{label}：{record[k]}\n')
        text=p['text']
        meta={k:v for k,v in p.items() if k not in ('text','source_visual_review','secondary_ocr')}
        meta.update(source_sha256=sha,length=len(text))
        parts.append('\n'+PAGE+packed(meta)+END+'\n'+text+'\n')
        if not text.strip():parts.append('\n（本页没有提取到文字。）\n')
    body=''.join(parts)
    first=packed(dict(source=source.name,sha256=sha,kind=kind,pages=len(pages),body_sha256=hashlib.sha256(body.encode()).hexdigest()))
    result=FORMAT+first+END+'\n'+body
    if path.exists() and path.read_text(encoding='utf-8')==result:return
    if fingerprint(source)!=sha:raise ValueError('处理期间原件已变化，待重新读取')
    path.parent.mkdir(exist_ok=True)
    atomic(path,result)
