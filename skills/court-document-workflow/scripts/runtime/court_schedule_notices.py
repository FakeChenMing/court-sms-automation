"""Conservative extraction of explicit appointments in court notice PDFs.
No deadlines are computed. Relative, conditional or ambiguous slots require review.
"""
import re
import html
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta

ACTIVITY = r'进场鉴定|现场鉴定|现场勘验|现场勘查|现场检测|证据交换|庭前会议|听证|调查|调解|询问|开庭|勘验|鉴定'
NOTICE_NAME = re.compile(r'通知|告知函|通知函|联系函|安排|传唤')
STATIC_NAME = re.compile(r'权利义务|风险|举证通知|受理|应诉|交纳|缴费|查控|执行通知|履行|鉴定方案|判决书|裁定书|笔录')
DATE_TIME = re.compile(
 r'(20\d{2})年(\d{1,2})月(\d{1,2})日(?:[（(](?:星期|周)[一二三四五六日天][）)])?'
 r'(上午|下午|中午|晚上|凌晨)?(\d{1,2})(?:[:：](\d{2})|[时点](?:(\d{1,2})分?|整)?)')

def plain_text(text):
    text = re.sub(r'</t[dh]>\s*<t[dh][^>]*>', '：', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', '', html.unescape(text)).replace('**','').replace('#','')
    return re.sub(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', r'\1年\2月\3日', text)

def named_candidate(path):
    name = path.stem
    return bool(NOTICE_NAME.search(name) and not STATIC_NAME.search(name))

def parse_notice(text, path, case_number, now=None):
    now = now or datetime.now()
    text = plain_text(text)
    relevant = named_candidate(path)
    # Content-based detection also covers neutral filenames containing a real notice.
    activity = re.search(ACTIVITY, path.stem) or re.search(ACTIVITY, text[:250])
    if not activity or not (relevant or re.search(r'(?:通知|告知函|定于|安排于|请于)', text[:600])):
        return {'status': 'irrelevant'}
    purpose = activity.group()
    if STATIC_NAME.search(path.stem):
        return {'status': 'irrelevant'}
    slots = []
    for match in DATE_TIME.finditer(text):
        prefix = text[max(0,match.start()-40):match.start()]
        # Only a scheduled action or explicit time label can anchor a date.
        if not re.search(r'定于|安排于|请于|将于|拟于|时间[:：]?$', prefix):
            continue
        if re.search(r'原定|原安排|曾定|此前|原时间', prefix):
            continue
        slots.append(match)
    if not slots:
        return {'status': 'manual', 'reason': '通知未识别到明确的完整年月日与时刻'} if relevant else {'status': 'irrelevant'}
    starts = set()
    try:
        for match in slots:
            year,month,day,period,hour,minute1,minute2=match.groups()
            hour=int(hour);minute=int(minute1 or minute2 or 0)
            if period in ('下午','中午','晚上') and hour<12:hour+=12
            elif period=='凌晨' and hour==12:hour=0
            starts.add(datetime(int(year),int(month),int(day),hour,minute))
    except ValueError:
        return {'status':'manual','reason':'日期或时刻无效'}
    if len(starts)!=1:
        return {'status':'manual','reason':'同一文件存在多个安排时间，需分别核对'}
    start=next(iter(starts))
    if start<=now:
        return {'status':'past','start':start.isoformat(),'reason':'安排时间已过，不补建'}
    tentative = '拟于' in text and not re.search(r'请[^。；]{0,40}(?:准时到场|准时参加|务必到场)', text)
    if tentative or re.search(r'取消|暂缓|延期|另行通知|时间待定|拟定于|暂定|如[^。；]{0,30}(?:同意|许可|确认|具备)', text):
        return {'status':'manual','reason':'存在取消、变更或不确定表述，需核对最终安排'}
    if not re.search(r'[（(]20\d{2}[）)].+号', case_number or ''):
        return {'status':'manual','reason':'案号未核实，不能自动归属案件'}
    # Extract a labelled venue, or the venue in the same scheduled-action sentence.
    venues=[]
    for match in re.finditer(r'(?:鉴定地点|勘验地点|听证地点|开庭地点|调查地点|集合地点|活动地点|地点|应到处所|应到场所)[:：]([^。；\n]{2,160}?)(?=时间[:：]|联系人|联系电话|注意事项|请各|请于|。|；|$)',text):
        venues.append(match.group(1).strip('，,|：:'))
    for match in slots:
        tail=text[match.end():match.end()+250]
        venue=re.match(r'(?:在|进入|前往|到达|至)(.{2,160}?)(?:，|,|开展|进行|参加)',tail)
        if venue:venues.append(venue.group(1).strip('，,|：:'))
    venues=set(venues)
    if len(venues)!=1:
        return {'status':'manual','reason':'地点缺失或存在多个地点，需人工核对'}
    venue=next(iter(venues))
    if any(x in venue for x in ('待定','另行','另通知','详见','届时')):
        return {'status':'manual','reason':'地点尚未明确'}
    end=start+timedelta(hours=1)
    evidence='；'.join(text[max(0,x.start()-15):x.end()+160] for x in slots)
    return {'status':'event','event':{
      'title':f'{case_number} {purpose}', 'start':start.isoformat(),'end':end.isoformat(),
      'location':venue,'description':f'案号：{case_number}\n事项：{purpose}\n来源：{path.name}\n原文片段：{evidence}\n结束时间为日历预留一小时，非法院或鉴定机构指定结束时间。',
      'cause':'','start_timestamp':str(start.timestamp()),'end_timestamp':str(end.timestamp())}}

def memory_fraction():
    total=int(subprocess.check_output(['/usr/sbin/sysctl','-n','hw.memsize'],text=True))
    stats=subprocess.check_output(['/usr/bin/vm_stat'],text=True)
    size=int(re.search(r'page size of (\d+) bytes',stats).group(1))
    values={k:int(v) for k,v in re.findall(r'^([^:]+):\s+(\d+)\.',stats,re.M)}
    return sum(values.get(k,0) for k in ('Pages active','Pages wired down','Pages occupied by compressor'))*size/total

def run_guarded_ocr(command, timeout=240):
    """Bound one OCR worker; stop our process group if physical memory reaches 90%."""
    import tempfile
    if memory_fraction()>=0.80:
        raise RuntimeError('OCR内存余量不足，保留待重试')
    with tempfile.TemporaryFile(mode='w+') as out, tempfile.TemporaryFile(mode='w+') as err:
        process=subprocess.Popen(command,stdout=out,stderr=err,text=True,start_new_session=True)
        started=time.monotonic()
        try:
            while process.poll() is None:
                if memory_fraction()>=0.90:raise RuntimeError('OCR内存达到90%，已停止本任务')
                if time.monotonic()-started>timeout:raise TimeoutError('OCR超时')
                time.sleep(1)
        except BaseException:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
            raise
        out.seek(0);err.seek(0)
        return subprocess.CompletedProcess(command,process.returncode,out.read(),err.read())
