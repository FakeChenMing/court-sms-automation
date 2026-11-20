"""Shared page reader for the interactive workflow and court workers."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pymupdf as fitz
import ocr_markdown as md

ROOT = Path(os.environ.get('QUARK_OCR_DIR', Path.home()/'Library/Application Support/QuarkOCR'))
URL = 'https://scan-business.quark.cn/vision'
VERSION = 'text-quark-v1'
CRITICAL = re.compile(r'\d[\d,.，]*\s*(?:年|月|日|时|元|%|号)|[A-Z0-9]{6,}|签收|签名|签字|盖章|印章|手写|落款')

def save(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode='w', encoding='utf-8') as stream:
        stream.write(content)
    os.replace(stream.name, path)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

def cloud(image, scene):
    if len(image)>5*1024**2:
        raise ValueError('单页超过夸克图像上限')
    key=os.environ.get('SCAN_WEBSERVICE_KEY') or (ROOT/'agent-key').read_text().strip()
    body={'aiApiKey':key,'dataType':'image','scene':scene,'dataBase64':base64.b64encode(image).decode()}
    req=Request(URL, data=json.dumps(body).encode(), headers={'Content-Type':'application/json','X-Appbuilder-From':'community-cli'})
    with build_opener(NoRedirect()).open(req, timeout=120) as response:
        reply=json.load(response)
    if reply.get('code') not in ('00000',0):
        raise RuntimeError('夸克调用失败 '+str(reply.get('code')))
    data=reply.get('data') or {}
    if 'OcrInfo' in data:
        blocks=data['OcrInfo']
        return '\n'.join(x.get('Text','') for x in blocks),{}, any(
            detail.get('Type')=='WrittenText' or (isinstance(detail.get('Confidence'),(float,int)) and detail['Confidence']<0.75)
            for block in blocks for detail in block.get('Detail',[]))
    if 'StructureInfo' in data:
        fields={k:v.get('Value','') for record in data['StructureInfo'] for k,v in record.items() if isinstance(v,dict)}
        return '\n'.join(f'{k}：{v}' for k,v in fields.items() if v),fields,False
    raise ValueError('夸克返回缺少识别结果')

def complete_layer(page,text):
    if not text.strip() or re.search(r'[\ufffd\x00-\x08\ue000-\uf8ff]',text):
        return False
    stamp_rects=[a.rect for a in (page.annots() or []) if a.type[1]=='Stamp']
    words=page.get_text('words')
    if not words:
        return False
    for item in page.get_image_info():
        rect=fitz.Rect(item['bbox']) & page.rect
        if rect.is_empty: continue
        fraction=rect.get_area()/max(page.rect.get_area(),1)
        # Small square QR codes and seal graphics still need original-page review.
        if fraction<0.06 and any(r.contains(rect) or (r & rect).get_area()>rect.get_area()*0.9 for r in stamp_rects): continue
        if fraction<0.018 and 0.7<rect.width/max(rect.height,1)<1.4: continue
        if fraction<0.001: continue
        overlay=[w for w in words if rect.contains(fitz.Point((w[0]+w[2])/2,(w[1]+w[3])/2))]
        if not overlay or fraction>0.2:
            # For a scanned background with a text overlay, check for uncovered
            # printed lines instead of treating a page number as a complete layer.
            pix=page.get_pixmap(matrix=fitz.Matrix(1,1),colorspace=fitz.csGRAY,clip=rect,alpha=False)
            from PIL import Image, ImageDraw
            mask=Image.frombytes('L',(pix.width,pix.height),pix.samples)
            draw=ImageDraw.Draw(mask)
            for w in overlay:
                r=fitz.Rect(w[:4])*page.rotation_matrix
                draw.rectangle((r.x0-pix.x-2,r.y0-pix.y-2,r.x1-pix.x+2,r.y1-pix.y+2),fill=255)
            if sum(mask.histogram()[:155])>max(20,rect.get_area()*0.0005): return False
    # Numerous outlined glyphs can represent text absent from the native layer.
    if sum(len(d['items']) for d in page.get_drawings() if d['type'] in ('f','fs'))>160 and len(text.strip())<40:
        return False
    return True

def classify(label):
    if re.search(r'铁路|火车|电子客票',label): return 'rail'
    if re.search(r'住宿|酒店|宾馆',label) and re.search(r'发票|住宿费',label): return 'hotel'
    return 'auto'

def read(source,kind='auto',allow_cloud=True):
    source=Path(source)
    source=source.absolute()
    with md.writing(source):
        digest=md.fingerprint(source)
        path=md.target(source)
        pages=md.read_companion(path,source,digest,kind)
        if pages is None and kind=='auto':
            pages=md.cached_court(digest)
        if pages is None:
            with fitz.open(source) as original:
                if original.is_pdf:
                    pages=read_pages(original,source,digest,kind,allow_cloud)
                else:
                    with fitz.open('pdf',original.convert_to_pdf()) as pdf:
                        pages=read_pages(pdf,source,digest,kind,allow_cloud)
        md.publish(path,source,digest,kind,pages)
        return pages

def read_pages(pdf,source,digest,kind,allow_cloud):
    pages=[]
    for page in pdf:
        native=page.get_text(sort=True).strip()
        label=kind if kind!='auto' else classify(native+' '+(source.stem if len(pdf)==1 else ''))
        directory=ROOT/'pages'/VERSION/digest/label
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        saved=directory/f'{page.number+1}.json'
        with (directory/f'{page.number+1}.lock').open('a') as lock:
            md.lock_file(lock)
            if saved.exists():
                pages.append(json.loads(saved.read_text()));continue
            for other in sorted(directory.parent.glob('*/'+saved.name)):
                candidate=json.loads(other.read_text())
                expected='夸克/'+('vat-invoice-ocr' if label=='hotel' else 'general-ocr')
                if (candidate.get('source_sha256')==digest and candidate.get('page')==page.number+1
                    and candidate.get('engine') in ('PDF文字层',expected)):
                    break
            else:
                candidate=None
            if candidate is not None:
                pages.append(candidate)
                continue
            fields={};anomaly=False;confidence_flag=False
            if complete_layer(page,native):
                text=native;engine='PDF文字层'
            else:
                if not allow_cloud: raise RuntimeError('等待首轮夸克识别')
                scene='vat-invoice-ocr' if label=='hotel' else 'general-ocr'
                for dpi in (180,150,120,96):
                    pix=page.get_pixmap(dpi=dpi,colorspace=fitz.csRGB,alpha=False)
                    image=pix.tobytes('jpeg',jpg_quality=91)
                    if len(image)<=5*1024**2: break
                text,fields,confidence_flag=cloud(image,scene)
                if label=='auto' and classify(text)=='hotel':
                    text,fields,confidence_flag=cloud(image,'vat-invoice-ocr')
                    scene='vat-invoice-ocr'
                engine='夸克/'+scene
                anomaly=confidence_flag or len(text.strip())<8 or bool(fields and not fields.get('价税合计小写'))
            original_review=bool(anomaly or CRITICAL.search(text) or page.first_annot is not None or any(0.002 < (fitz.Rect(x['bbox']) & page.rect).get_area()/max(page.rect.get_area(),1) < 0.3 for x in page.get_image_info()) or re.search('签收|签章|手写',source.stem))
            item=dict(page=page.number+1,text=text,engine=engine,needs_visual_review=original_review,
                      ocr_anomaly=anomaly,source_sha256=digest,fields=fields)
            save(saved,json.dumps(item,ensure_ascii=False))
            pages.append(item)
    return pages

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('path')
    parser.add_argument('--kind',choices=['auto','hotel','rail'],default='auto')
    parser.add_argument('--review-record',type=Path,help='已实际查看原图的逐页复核记录 JSON')
    args=parser.parse_args()
    if args.review_record:
        md.record_review(Path(args.path),json.loads(args.review_record.read_text()))
    print(json.dumps(read(args.path,args.kind),ensure_ascii=False,indent=2))
