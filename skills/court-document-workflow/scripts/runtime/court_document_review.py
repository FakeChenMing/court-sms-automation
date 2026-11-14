"""Durable, source-bound OCR and legal-calendar review ledger. No legal guessing."""
from __future__ import annotations
from pathlib import Path
from contextlib import contextmanager,closing
from datetime import datetime,date,timedelta,timezone
import json,hashlib,re,fcntl,os,shutil,argparse
import sqlite3
STATE_DIR=Path(os.environ.get('COURT_STATE_DIR',str(Path.home()/'Library/Application Support/CourtSMSMonitor')))
ROOT=Path(os.environ.get('COURT_DOWNLOAD_ROOT',str(Path.home()/'Desktop/法院材料汇总')))
LEDGER=STATE_DIR/'document_review.json'
ATTACHMENT_HELPER=Path(__file__).with_name('CourtEventAttachment.app')/'Contents/MacOS/court_event_attachment'
CALENDAR_POLICY_VERSION=4
CHINA_TZ=timezone(timedelta(hours=8))
CALENDAR_DB=Path.home()/'Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb'
CALENDAR_NAME=os.environ.get('COURT_CALENDAR_NAME','法院工作日历')

def run_attachment_helper(mode,plan_path,timeout=180):
    """Launch the signed app normally; open's exit status is not a helper receipt."""
    import subprocess,tempfile
    assert mode in ('review-inspect','review-sync'), '不支持的附件调用模式'
    plan_path=Path(plan_path).resolve();plan=json.loads(plan_path.read_text())
    app=ATTACHMENT_HELPER.parents[2]
    assert app.suffix=='.app' and app.is_dir() and ATTACHMENT_HELPER.is_file(), '附件应用不可用'
    def diagnostic(reason,code=None,output='',open_code=None,helper_error=False):
        stamp=datetime.now().isoformat()
        value={'checked_at':stamp,'mode':mode,'uid':plan.get('uid'),'event_key':plan.get('event_key'),
               'reason':reason,'helper_stderr_present':helper_error,'output_bytes':len(output.encode()),
               'output_sha256':hashlib.sha256(output.encode()).hexdigest()}
        if code is not None:value['result_code']=code
        if open_code is not None:value['open_returncode']=open_code
        target=STATE_DIR/'attachment_transport'/('diagnostic-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
        atomic(target,value)
        return str(target)
    with tempfile.TemporaryDirectory(prefix='court-attachment-app-') as temp:
        stdout=Path(temp)/'stdout.json';stderr=Path(temp)/'stderr.log'
        command=['/usr/bin/open','-n','-W','--stdout',str(stdout),'--stderr',str(stderr),str(app),'--args',mode,str(plan_path)]
        try:launched=subprocess.run(command,capture_output=True,text=True,timeout=timeout)
        except (subprocess.TimeoutExpired,OSError) as exc:
            diagnostic('launch_timeout' if isinstance(exc,subprocess.TimeoutExpired) else 'launch_unavailable')
            raise
        output=stdout.read_text() if stdout.exists() else ''
        open_error=(launched.stderr or '').strip()
        helper_error=(stderr.read_text() if stderr.exists() else '').strip()
        wait_race=open_error=='Unable to block on application (GetProcessPID() returned 18446744073709551016)' and not helper_error
        errors='\n'.join(x for x in (open_error,helper_error) if x)
        if (launched.returncode or errors) and not wait_race:
            code=next((code for phrase,code in (
                ('日历附件权限不可用',77),('已有同名附件内容暂不可核验',80),
                ('附件目标与日历回读内容不一致',70),('事项标识及时间未唯一匹配',70),
                ('未从日历读回内容一致的PDF附件',75),('未读回要求的提前提醒',74),
                ('既有提醒未保留',74),('日期依据不是可读取 PDF',66)) if phrase in errors),76)
            result=subprocess.CompletedProcess(command,code,output,errors or '附件应用启动未成功')
            result.transport_diagnostic_path=diagnostic('helper_stderr' if helper_error else 'open_failed',code,output,launched.returncode,bool(helper_error))
            return result
        try:
            value=json.loads(output)
            assert isinstance(value,dict) and value.get('uid')==plan.get('uid') and value.get('event_key','')==plan.get('event_key',''), '附件应用回执对象不符'
            verify_extras_receipt(plan,value)
        except (ValueError,TypeError,KeyError,AssertionError):
            result=subprocess.CompletedProcess(command,76,'','附件应用未返回完整、匹配且通过核验的JSON回执')
            result.transport_diagnostic_path=diagnostic('wait_race_invalid_receipt' if wait_race else 'invalid_receipt',76,output,launched.returncode)
            return result
        if wait_race:
            warning='launchservices_wait_process_race'
            path=diagnostic(warning,0,output,launched.returncode)
            value.update(transport_warning=warning,transport_diagnostic_path=path)
            result=subprocess.CompletedProcess(command,0,json.dumps(value,ensure_ascii=False),'')
            result.transport_warning=warning;result.transport_diagnostic_path=path
            return result
        return subprocess.CompletedProcess(command,0,output,'')

def canonical_case(value):
    value=re.sub(r'\s+','',value or '').replace('(','（').replace(')','）')
    return value if re.fullmatch(r'（20\d{2}）[\u4e00-\u9fffA-Za-z0-9]+号',value) else ''

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

@contextmanager
def transaction():
    STATE_DIR.mkdir(parents=True,exist_ok=True)
    with (STATE_DIR/'document_review.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if LEDGER.exists():
            data=json.loads(LEDGER.read_text());assert data.get('version')==1 and isinstance(data.get('documents'),dict)
        else:data={'version':1,'documents':{},'cases':{},'events':{}}
        yield data
        if LEDGER.exists():shutil.copy2(LEDGER,LEDGER.with_suffix('.previous.json'))
        atomic(LEDGER,data)

def wake_independent_worker():
    import subprocess
    label=os.environ.get('COURT_WORKFLOW_LAUNCHD_LABEL')
    if not label:return
    try:
        subprocess.run(['/bin/launchctl','kickstart',f'gui/{os.getuid()}/{label}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
    except (OSError,subprocess.TimeoutExpired):
        pass # Persistent queue + launchd StartInterval recover a missed wake.

def enqueue_documents(case_number,files,received_at,source_rowid=None):
    if received_at and received_at[:10]<'2026-01-01':raise ValueError('不处理2026年前收到的短信')
    case=canonical_case(case_number);ids=[]
    with transaction() as ledger:
        for value in files:
            path=Path(value).resolve();sha=digest(path)
            key=hashlib.sha256(((case or 'unknown:'+str(path.parent))+'|'+sha).encode()).hexdigest()[:32]
            docs=ledger['documents'];new=key not in docs
            item=docs.setdefault(key,{'id':key,'case_number':case,'sha256':sha,'paths':[],'deliveries':[],
                'ocr_status':'pending','review_status':'pending','created_at':datetime.now().isoformat(), 'events':[]})
            if str(path) not in item['paths']:item['paths'].append(str(path))
            delivery={'received_at':received_at,'source_rowid':source_rowid}
            added_delivery=delivery not in item['deliveries']
            if added_delivery:item['deliveries'].append(delivery)
            ids.append(key)
            if (new or added_delivery) and case:
                context=ledger['cases'].setdefault(case,{'revision':0})
                context['revision']=context.get('revision',0)+1
                # Later files can clarify names/roles or replace an earlier arrangement.
                for old in docs.values():
                    if old['case_number']==case and old['review_status'] in ('complete','attention'):old['context_refresh_needed']=True
    if ids:wake_independent_worker()
    return ids

def register_ocr(docid,text_path,engine,skill_path,page_count,source_hash,uncertain_pages=None):
    text_path=Path(text_path).resolve();assert text_path.is_file() and text_path.stat().st_size>0
    assert engine and skill_path and Path(skill_path).is_file() and int(page_count)>0
    with transaction() as data:
        doc=data['documents'][docid];assert doc['sha256']==source_hash
        assert any(Path(p).is_file() and digest(p)==source_hash for p in doc['paths'])
        doc.update(ocr_status='partial' if uncertain_pages else 'complete',ocr={'text_path':str(text_path),'text_sha256':digest(text_path),'engine':engine,'skill_path':str(Path(skill_path).resolve()),'page_count':int(page_count),'uncertain_pages':uncertain_pages or [],'verified_at':datetime.now().isoformat()},review_status='pending')

def attachment_manifest(event):
    paths=event.get('attachment_paths')
    assert isinstance(paths,list) and paths, '日程缺少日期依据 PDF 附件'
    files=[]
    for value in dict.fromkeys(paths):
        assert isinstance(value,str), '附件路径无效'
        path=Path(value).resolve()
        assert path.is_file() and path.suffix.lower()=='.pdf', '附件不是可读取 PDF'
        with path.open('rb') as source:assert source.read(5)==b'%PDF-', '附件内容不是 PDF'
        files.append({'source_path':str(path),'sha256':digest(path)})
    return files

def calendar_event_requirements(event):
    assert event.get('basis_type') in ('document_explicit','calculated','document_received') or (event.get('basis_type')=='internal_check' and event.get('metadata_only') and event.get('existing_uid')), '待核事项只能留在待核台账；既有待核日程仅允许内容修复'
    assert (event.get('basis_type')=='document_received') == (event.get('event_kind')=='receipt'), '收到节点与收件依据类型不一致'
    if event.get('metadata_only'):assert event.get('existing_uid'), '内容修复必须指定已有 UID'
    if event.get('is_deadline') or event.get('event_kind')=='deadline':assert event.get('alarm_minutes')==-10080, '到期事项须设置提前 7 日提醒'
    if event.get('alarm_minutes') is not None:
        assert isinstance(event['alarm_minutes'],int) and not isinstance(event['alarm_minutes'],bool) and event['alarm_minutes']<0, '提前提醒分钟值无效'
    return attachment_manifest(event)

def received_timestamp(value):
    assert isinstance(value,str) and value, '缺少收件时间'
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    # Older delivery manifests use local timestamps without a UTC offset.
    return stamp.replace(tzinfo=CHINA_TZ) if stamp.tzinfo is None else stamp.astimezone(CHINA_TZ)

def checked_date(value,label):
    assert isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value), label+'必须是完整日期'
    assert date.fromisoformat(value).isoformat()==value, label+'无效'
    return value

def validate_received_binding(evidence,document,received):
    assert isinstance(evidence,dict) and evidence.get('kind')=='delivery_sms', '缺少对应文书的已核对收件记录'
    assert evidence.get('source_doc')==document['id'] and evidence.get('document_sha256')==document['sha256'], '收件记录与文书不匹配'
    assert evidence.get('binding_kind') in ('download_manifest','download_job') and isinstance(evidence.get('binding_source'),str) and evidence['binding_source'].strip(), '收件记录缺少精确下载关联来源'
    proof=document.get('receipt_binding')
    assert document.get('receipt_binding_verified') is True and isinstance(proof,dict) and all(proof.get(k)==v for k,v in evidence.items()), '缺少已核实精确收件绑定；旧同案周匹配不能作为本件收件证明'
    rowid=evidence.get('source_rowid')
    assert not isinstance(rowid,bool) and str(rowid or '').isdigit() and int(rowid)>0, '收件记录缺少真实短信行号'
    stamp=received_timestamp(evidence.get('received_at'))
    assert stamp.date().isoformat()==received and stamp<=datetime.now(CHINA_TZ), '收到日期与记录不一致或位于未来'
    deliveries=[d for d in document.get('deliveries',[]) if str(d.get('source_rowid') or '').isdigit() and int(d['source_rowid'])>0 and d.get('received_at')]
    assert any(str(d['source_rowid'])==str(rowid) and received_timestamp(d['received_at'])==stamp for d in deliveries), '收件记录未与本件下载关联核对'
    # Old deliveries can contain case/week guesses; only the exact binding counts.

def receipt_group_key(case_number,received_date):
    case=canonical_case(case_number);assert case, '收到群组案号无效'
    return case+'|收到材料|'+checked_date(received_date,'收到日期')

def validate_receipt_documents(event,documents,data=None,require_review=False):
    members=event.get('receipt_documents')
    assert isinstance(members,list) and members, '收到群组缺少逐份文书清单'
    seen=set();attached={(x.get('source_path'),x.get('sha256')) for x in event.get('attachment_manifest',[])}
    for member in members:
        assert isinstance(member,dict) and member.get('id') in documents and member['id'] not in seen, '收到群组文书缺失或重复'
        seen.add(member['id']);doc=documents[member['id']]
        assert doc['case_number']==event['case_number'] and member.get('sha256')==doc['sha256'], '收到群组混入不同案号或原件'
        path=Path(member.get('source_path') or '').resolve()
        assert str(path) in {str(Path(p).resolve()) for p in doc['paths']} and path.is_file() and digest(path)==doc['sha256'], '群组文书路径或内容与台账不符'
        assert (str(path),doc['sha256']) in attached, '收到群组缺少某份原件PDF附件'
        validate_received_binding(member.get('binding'),doc,event['received_date'])
        assert isinstance(member.get('source_page'),int) and not isinstance(member['source_page'],bool) and member['source_page']>0, '群组文书缺少来源页'
        assert isinstance(member.get('source_quote'),str) and member['source_quote'].strip() and isinstance(member.get('material_type'),str) and member['material_type'].strip(), '群组文书缺少原句或类型'
        if require_review:validate_receipt_member_review(member,doc,data)
    if event.get('received_evidence') is not None:assert any(m['binding']==event['received_evidence'] for m in members), '群组摘要收件依据不属于成员'
    return members

def validate_temporal_metadata(event,document,documents=None):
    """The operational received date never substitutes for a legal anchor."""
    kind=event.get('event_kind') or ('deadline' if event.get('is_deadline') else 'appointment')
    assert kind in ('receipt','deadline','appointment'), '日程类型无效'
    if kind not in ('receipt','deadline'):
        assert event.get('basis_type')!='document_received', '收到记录必须使用receipt类型'
        return
    received=checked_date(event.get('received_date'),'收到日期')
    assert received>='2026-01-01', '不补建2026年前的收到节点'
    if kind=='receipt':validate_receipt_documents(event,documents or {document['id']:document})
    else:validate_received_binding(event.get('received_evidence'),document,received)
    receipt_key=receipt_group_key(document['case_number'],received)
    start=datetime.fromisoformat(event['start']);end=datetime.fromisoformat(event['end'])
    assert start.utcoffset()==timedelta(hours=8) and end.utcoffset()==timedelta(hours=8), '双节点日程必须使用北京时间'
    if kind=='receipt':
        assert event.get('event_key')==receipt_key, '收到节点必须以完整案号和收到日期稳定去重'
        assert event.get('all_day') is True and start.date().isoformat()==received and start.time().isoformat()=='00:00:00', '收到节点须登记在真实收到当天'
        assert (end-start).total_seconds() in (86399,86400), '收到节点仅占收到当天'
        assert not event.get('is_deadline') and event.get('alarm_minutes') is None, '收到节点不能冒充到期提醒'
        dates=event.get('due_dates');pending=event.get('pending_deadlines')
        assert isinstance(dates,list) and isinstance(pending,list) and all(isinstance(x,str) and x.strip() for x in pending), '收到节点须列明已知到期日和待核期限'
        keys=set()
        for item in dates:
            assert isinstance(item,dict) and isinstance(item.get('subject'),str) and item['subject'].strip(), '到期事项缺少名称'
            checked_date(item.get('date'),'到期日期')
            assert isinstance(item.get('event_key'),str) and item['event_key'] and item['event_key']!=receipt_key and item['event_key'] not in keys, '到期事项缺少唯一关联标识'
            keys.add(item['event_key'])
    else:
        assert event.get('basis_type') in ('document_explicit','calculated'), '到期节点必须有明确或经核算的日期依据'
        assert checked_date(event.get('due_date'),'到期日期')==start.date().isoformat(), '到期日期必须等于到期节点日期'
        assert event.get('paired_receipt_key')==receipt_key, '到期节点缺少同文书收到节点关联'

def allows_historical_node(event):
    return event.get('event_kind')=='receipt' or (
        event.get('event_kind')=='deadline' and event.get('retrospective_pair') is True
        and bool(event.get('paired_receipt_key')) and event.get('due_date')==event['start'][:10])

def verify_extras_receipt(event,receipt):
    assert receipt.get('extras_verified') is True and receipt.get('metadata_preserved') is True, '附件及提醒尚未回读'
    actual={(x.get('source_path'),x.get('sha256')) for x in receipt.get('attachments',[]) if x.get('content_verified') is True}
    expected={(x['source_path'],x['sha256']) for x in event['attachment_manifest']}
    assert expected<=actual, '附件回读内容与日期依据不一致'
    if event.get('alarm_minutes') is not None:
        assert any(x.get('absolute_timestamp') is None and x.get('relative_minutes')==event['alarm_minutes'] for x in receipt.get('alarms',[])), '未回读要求的提前提醒'
    scope=receipt.get('attachment_sync_scope')
    if scope=='icloud':
        attachments=receipt.get('attachments',[])
        assert attachments and all(isinstance(item.get('remote_available'),bool) for item in attachments), '缺少逐附件远端状态'
        inventory=receipt.get('attachment_inventory',attachments)
        assert isinstance(inventory,list) and inventory and all(isinstance(item.get('remote_available'),bool) for item in inventory), '缺少完整附件远端状态'
        uploaded=all(item['remote_available'] for item in inventory)
        assert receipt.get('attachment_sync_state')==('uploaded' if uploaded else 'pending'), '附件云端状态与回读不符'
        assert receipt.get('sync_pending') is (not uploaded), '附件待同步状态不符'
        assert receipt.get('attachment_sync_evidence')=='remote_url_registered', '附件云端核验依据不明'
    elif scope in ('local','other'):
        assert 'attachment_sync_state' not in receipt and 'sync_pending' not in receipt, '不可对本地或未知服务宣称iCloud同步状态'
    else:
        assert scope is None and 'attachment_sync_state' not in receipt and 'sync_pending' not in receipt, '附件同步服务无法识别'

def record_review(docid,decision):
    with transaction() as data:
        doc=data['documents'][docid]
        assert doc['ocr_status'] in ('complete','partial'), 'OCR尚未执行'
        if doc['ocr_status']=='partial':assert set(doc['ocr'].get('uncertain_pages',[]))<=set(decision.get('visual_reviewed_pages',[])), 'OCR不清晰页尚未逐页复核原图'
        assert Path(doc['ocr']['text_path']).is_file() and digest(doc['ocr']['text_path'])==doc['ocr']['text_sha256'], 'OCR成果已变化'
        assert any(Path(p).is_file() and digest(p)==doc['sha256'] for p in doc['paths']), '原件已变化'
        assert decision.get('source_sha256')==doc['sha256']
        assert decision.get('all_pages_reviewed') is True and decision.get('date_inventory') is not None
        assert decision.get('conclusion') and isinstance(decision.get('events'),list)
        assert decision.get('calendar_policy_version')==CALENDAR_POLICY_VERSION or not decision['events'], '旧版日程方案须重新审查'
        # Every proposed event requires auditable source evidence; no literal date scan is sufficient.
        for event in decision['events']:
            event['attachment_manifest']=calendar_event_requirements(event)
            validate_temporal_metadata(event,doc,data['documents'])
            if event.get('event_kind')=='receipt':
                assert docid in {m['id'] for m in event['receipt_documents']}, '收到群组未包含所属全文文书'
                review_data={**data,'documents':{**data['documents'],docid:{**doc,'decision':decision}}}
                validate_receipt_documents(event,review_data['documents'],review_data,require_review=True)
            event['attachment_paths']=[x['source_path'] for x in event['attachment_manifest']]
            assert canonical_case(event.get('case_number'))==doc['case_number'] and doc['case_number']
            assert event.get('source_quote') and event.get('source_page') and event.get('category')
            assert event.get('basis_type') in ('document_explicit','calculated','internal_check','document_received')
            assert event.get('parties_status') in ('verified','partial','unknown','conflict')
            if event['parties_status'] in ('verified','partial'):assert event.get('parties') and event.get('party_sources')
            if event['basis_type']=='calculated':
                assert event.get('anchor_evidence') and event.get('calculation') and event.get('legal_sources')
                assert event.get('holiday_check') and event.get('cross_check')
            if event['basis_type']=='internal_check':assert '待核' in event.get('title','') and event.get('uncertainty')
            assert event.get('start') and event.get('end') and datetime.fromisoformat(event['end'])>datetime.fromisoformat(event['start'])
            dt=datetime.fromisoformat(event['start']);assert dt.tzinfo is not None, '必须指定时区'
            now=datetime.now(dt.tzinfo)
            assert (dt.date()>=now.date() if event.get('all_day') else dt>now) or (event.get('metadata_only') and event.get('existing_uid')) or allows_historical_node(event), '过去安排不补建；仅授权收到/已核实双节点补齐'
            assert '法院材料事项ID：'+event['event_key'] in event.get('notes','')
            assert '当事人' in event.get('notes','') or any(x in event.get('notes','') for x in ('原告','上诉人','申请执行人','申请人'))
            assert event.get('event_key') # stable matter+type+subject+source-purpose identity, not just time
        doc['decision']=decision;doc['review_status']='ready' if decision['events'] else ('attention' if decision.get('unresolved') else 'complete')
        expected_revision=decision.get('context_revision')
        current_revision=data['cases'].get(doc['case_number'],{}).get('revision',0)
        doc['context_refresh_needed']=expected_revision is not None and expected_revision!=current_revision
        doc['reviewed_at']=datetime.now().isoformat()

def validate_material_page_review(document,decision,event):
    """A receipt-only page check never claims that every document page was reviewed."""
    review=decision.get('material_page_review')
    assert isinstance(review,dict) and review.get('method')=='visual', '收到节点须有材料类型页的实际原图核对'
    assert review.get('source_sha256')==document['sha256']==decision.get('source_sha256'), '材料类型核对与原件不匹配'
    assert isinstance(review.get('page'),int) and not isinstance(review['page'],bool) and review['page']>0 and event.get('source_page')==review['page'], '材料类型核对页码不一致'
    assert isinstance(review.get('source_quote'),str) and review['source_quote'].strip() and event.get('source_quote')==review['source_quote'], '材料类型核对原句不一致'
    assert isinstance(review.get('material_type'),str) and review['material_type'].strip(), '材料类型尚未核实'
    assert received_timestamp(review.get('verified_at'))<=datetime.now(CHINA_TZ), '材料类型核对时间无效'
    path=Path(review.get('review_record') or '')
    assert path.is_file() and path.stat().st_size>0, '缺少原图核对记录'
    if review.get('review_record_sha256') is not None:assert digest(path)==review['review_record_sha256'], '原图核对记录已变化'
    assert any(Path(p).is_file() and digest(p)==document['sha256'] for p in document['paths']), '原件已变化'
    return digest(path)

def validate_receipt_member_review(member,document,data):
    basis=member.get('review_basis')
    if basis=='material_page_review':
        evidence=member.get('material_page_review')
        result=validate_material_page_review(document,{'source_sha256':document['sha256'],'material_page_review':evidence},member)
        evidence['review_record_sha256']=result
        return
    def same_source(item):
        return item.get('source_page')==member['source_page'] and item.get('source_quote')==member['source_quote']
    if basis=='verified_receipt':
        key=member.get('receipt_event_key');rec=data.get('events',{}).get(key,{})
        assert rec.get('verified') is True and rec.get('extras_verified') is True, '成员原收到日程尚未完成附件回读'
        candidates=[e for slot in ('decision','receipt_decision') for e in document.get(slot,{}).get('events',[]) if e.get('event_key')==key and e.get('event_kind')=='receipt']
        group=data.get('receipt_groups',{}).get(key,{})
        candidates += group.get('receipt_decision',{}).get('events',[])
        assert any(same_source(e) or any(m.get('id')==member['id'] and same_source(m) for m in e.get('receipt_documents',[])) for e in candidates), '成员原收到审查来源不符'
        assert any(x.get('sha256')==document['sha256'] and x.get('content_verified') is True for x in rec.get('attachments',[])), '成员原收到附件未核验对应原件'
        return
    assert basis=='complete_document_review', '群组成员缺少已核实材料类型依据'
    decision=document.get('decision',{})
    assert decision.get('source_sha256')==document['sha256'] and decision.get('all_pages_reviewed') is True, '成员全文尚未核实'
    assert document.get('ocr_status') in ('complete','partial'), '成员OCR尚未完成审查'
    if document['ocr_status']=='partial':assert set(document['ocr'].get('uncertain_pages',[]))<=set(decision.get('visual_reviewed_pages',[])), '成员不清晰页尚未实际复核'
    inventory=decision.get('events',[])+decision.get('date_inventory',[])+decision.get('receipt_materials',[])
    assert any(same_source(x) for x in inventory), '成员全文审查缺少对应材料来源页原句'

def validate_receipt_decision(data,decision,document=None):
    assert decision.get('calendar_policy_version')==CALENDAR_POLICY_VERSION, '旧版收到节点方案须重新审查'
    assert decision.get('receipt_only') is True and decision.get('all_pages_reviewed') is not True, '局部收件核对不得冒称全文审查完成'
    events=decision.get('events')
    assert isinstance(events,list) and len(events)==1 and events[0].get('event_kind')=='receipt', '独立收件核对仅允许一条收到节点'
    event=events[0]
    assert canonical_case(event.get('case_number'))==event.get('case_number'), '收到节点案号不符'
    assert event.get('category') and event.get('parties_status') in ('verified','partial','unknown','conflict')
    if event['parties_status'] in ('verified','partial'):assert event.get('parties') and event.get('party_sources')
    assert '法院材料事项ID：'+event['event_key'] in event.get('notes','')
    assert '当事人' in event.get('notes','') or any(x in event.get('notes','') for x in ('原告','上诉人','申请执行人','申请人'))
    event['attachment_manifest']=calendar_event_requirements(event)
    members=validate_receipt_documents(event,data['documents'],data,require_review=True)
    primary=data['documents'][members[0]['id']]
    if document is not None:assert document['id'] in {m['id'] for m in members}, '收到群组未包含所属文书'
    validate_temporal_metadata(event,primary,data['documents'])
    event['attachment_paths']=[x['source_path'] for x in event['attachment_manifest']]
    if event.get('existing_uid'):
        assert isinstance(event.get('expected_previous_notes'),str) and isinstance(event.get('expected_previous_title'),str), '复用收到UID须提供原标题备注快照'
        manual=manual_calendar_notes(event['expected_previous_notes'])
        assert not manual or manual in event['notes'], '群组合并不得丢失主日程人工备注'
    return event

def manual_calendar_notes(notes):
    begin='【法院材料自动登记】';end='【自动登记结束】'
    if begin not in notes:return notes.strip()
    before,managed=notes.split(begin,1)
    assert end in managed, '自动备注边界不完整，须人工核对后合并'
    return '\n\n'.join(x for x in (before.rstrip(),managed.split(end,1)[1].strip()) if x)

def record_receipt_review(docid,decision):
    """Register a verified received-day node without completing legal/document review."""
    with transaction() as data:
        doc=data['documents'][docid]
        validate_receipt_decision(data,decision,doc)
        doc['receipt_decision']=decision;doc['receipt_review_status']='ready'
        doc['receipt_reviewed_at']=datetime.now().isoformat()

def record_receipt_group_review(group_key,decision):
    with transaction() as data:
        event=validate_receipt_decision(data,decision)
        assert event['event_key']==group_key, '收到群组标识不符'
        group=data.setdefault('receipt_groups',{}).setdefault(group_key,{'group_key':group_key})
        old_members={(m['id'],m['sha256']) for old in group.get('receipt_decision',{}).get('events',[]) for m in old.get('receipt_documents',[])}
        assert old_members<={(m['id'],m['sha256']) for m in event['receipt_documents']}, '收到群组不得遗漏已登记成员'
        previous=data['events'].get(group_key,{})
        if previous.get('verified'):
            assert event.get('existing_uid')==previous['uid'], '已存在的群组须复用已核实主UID'
        group.update(case_number=event['case_number'],received_date=event['received_date'],receipt_decision=decision,receipt_review_status='ready',receipt_reviewed_at=datetime.now().isoformat())

def record_receipts(docid,receipts,receipt_only=False,receipt_group=False):
    with transaction() as data:
        doc=data['receipt_groups' if receipt_group else 'documents'][docid];decision=doc['receipt_decision' if receipt_only else 'decision'];events=decision['events']
        for event in events:
            rec=receipts.get(event['event_key']);assert rec and rec.get('verified') is True
            assert rec.get('title')==event['title'] and rec.get('start')==event['start']
            assert rec.get('end')==event['end'] and rec.get('location','')==event.get('location','')
            assert rec.get('notes')==event['notes'] and rec.get('uid') and rec.get('match_count')==1
            verify_extras_receipt(event,rec)
            data['events'][event['event_key']]=rec
        doc['receipt_events' if receipt_only else 'events']=[x['event_key'] for x in events]
        doc['receipt_review_status' if receipt_only else 'review_status']='attention' if decision.get('unresolved') else 'complete'

def calendar_marker_snapshot(events):
    """Optional read-only lookup hint. Calendar/JXA remains the writer and verifier."""
    keys={event['event_key'] for event in events}
    if any(not isinstance(key,str) or '\n' in key or '\r' in key for key in keys):return None
    matches={key:[] for key in keys}
    markers={'法院材料事项ID：'+key:key for key in keys}
    try:
        # mode=ro also prevents creating a missing database; never use immutable
        # because Calendar's current commits can still reside in its WAL.
        with closing(sqlite3.connect(CALENDAR_DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as connection:
            calendars=list(connection.execute('SELECT ROWID FROM Calendar WHERE title=?',(CALENDAR_NAME,)))
            if len(calendars)!=1:return None
            rows=list(connection.execute('SELECT UUID, summary, description FROM CalendarItem WHERE calendar_id=?',(calendars[0][0],)))
        for uid,title,notes in rows:
            if notes is None:continue
            if not isinstance(notes,str):return None
            # Complete marker lines only; a shorter event key cannot match a
            # different, longer key sharing its prefix.
            found=set(notes.splitlines())&markers.keys()
            if found and (not isinstance(uid,str) or not uid or not isinstance(title,str)):return None
            for marker in found:matches[markers[marker]].append({'uid':uid,'title':title,'notes':notes})
        return {'schema_version':1,'calendar_name':CALENDAR_NAME,'by_event_key':matches}
    except (sqlite3.Error,OSError,ValueError,TypeError):
        # Unsupported schema or missing permission falls back to the established
        # Apple Events lookup. It never weakens the final JXA readback.
        return None

def apply_receipt_group_calendar(group_key,fast_lookup=False):
    return apply_calendar(group_key,receipt_only=True,fast_lookup=fast_lookup,_receipt_group=True)

def refresh_attachment_sync(event_key):
    """Inspect a saved exact event again; never add, replace or save attachments."""
    import subprocess,tempfile
    with (STATE_DIR/'calendar_review.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=json.loads(LEDGER.read_text());previous=data.get('events',{}).get(event_key,{})
        assert previous.get('verified') is True and previous.get('metadata_verified') is True and not previous.get('calendar_removed_by_receipt_merge'), '仅重查仍在日历中的已核验事项'
        candidates=list(data.get('receipt_groups',{}).values())+list(data.get('documents',{}).values())
        event=next((e for item in candidates for slot in ('receipt_decision','decision') for e in item.get(slot,{}).get('events',[]) if e.get('event_key')==event_key),None)
        assert event is not None, '缺少已核验事项的原始附件方案'
        verify_extras_receipt(event,previous)
        assert calendar_event_requirements(event)==event.get('attachment_manifest'), '重查前原PDF发生变化'
        plan={**event,**receipt_snapshot(previous),'calendar':CALENDAR_NAME,
              'start_timestamp':datetime.fromisoformat(previous['start_utc'].replace('Z','+00:00')).timestamp(),
              'end_timestamp':datetime.fromisoformat(previous['end_utc'].replace('Z','+00:00')).timestamp()}
        with tempfile.TemporaryDirectory(prefix='calendar-sync-inspect-') as temp:
            path=Path(temp)/'inspect.json';path.write_text(json.dumps(plan,ensure_ascii=False))
            result=run_attachment_helper('review-inspect',path)
        if result.returncode:raise RuntimeError('附件同步状态重查失败：'+result.stderr[:300])
        extra=json.loads(result.stdout)
        assert extra.get('uid')==previous['uid'] and extra.get('event_key')==event_key, '附件重查回执对象不符'
        verify_extras_receipt(event,extra)
        assert extra.get('attachment_sync_scope') in ('icloud','local','other'), '附件核验程序尚未提供同步状态'
        refreshed={**previous,**extra}
        # Clear obsolete cloud fields if the event was moved into a local/other service.
        if extra['attachment_sync_scope']!='icloud':
            for field in ('attachment_sync_state','sync_pending','attachment_sync_evidence','attachment_sync_checked_at'):refreshed.pop(field,None)
        with transaction() as state:
            current=state.get('events',{}).get(event_key,{})
            assert current==previous, '重查期间台账已更新，保留最新记录'
            state['events'][event_key]=refreshed
            for bucket in ('documents','receipt_groups'):
                for item in state.get(bucket,{}).values():
                    for slot in ('partial_receipts','receipt_partial_receipts'):
                        if item.get(slot,{}).get(event_key,{}).get('uid')==previous['uid']:item[slot][event_key]=refreshed
        return refreshed

def apply_calendar(docid,receipt_only=False,fast_lookup=False,_receipt_group=False):
    import subprocess,tempfile
    with (STATE_DIR/'calendar_review.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        bucket='receipt_groups' if _receipt_group else 'documents'
        data=json.loads(LEDGER.read_text());doc=data[bucket][docid]
        decision=doc['receipt_decision' if receipt_only else 'decision']
        status_key='receipt_review_status' if receipt_only else 'review_status'
        partial_key='receipt_partial_receipts' if receipt_only else 'partial_receipts'
        assert doc[status_key]=='ready'
        assert decision.get('calendar_policy_version')==CALENDAR_POLICY_VERSION, '旧版待写方案须重新审查，不直接恢复写入'
        if receipt_only:
            before_manifest=decision['events'][0].get('attachment_manifest')
            validate_receipt_decision(data,decision,None if _receipt_group else doc)
            assert decision['events'][0]['attachment_manifest']==before_manifest, '日期附件在审查后发生变化'
        for event in decision['events']:
            source_doc=data['documents'][event['receipt_documents'][0]['id']] if _receipt_group else doc
            validate_temporal_metadata(event,source_doc,data['documents'])
            assert calendar_event_requirements(event)==event.get('attachment_manifest'), '日期附件在审查后发生变化'
        assert ATTACHMENT_HELPER.is_file(), 'PDF附件及提醒核验程序不可用'
        with tempfile.TemporaryDirectory(prefix='calendar-review-') as temp:
            plan=Path(temp)/'plan.json';receipt=Path(temp)/'receipts.json'
            planned_events=[];receipts={}
            for event in decision['events']:
                candidate=dict(event)
                candidate.pop('trusted_calendar_lookup',None)
                previous=doc.get(partial_key,{}).get(event['event_key'],{})
                if previous.get('uid'):
                    # Metadata may already be saved even when attachment readback
                    # failed. Inspect the complete original plan before any write.
                    error_kind='partial_metadata_mismatch';helper_code=None
                    try:
                        assert not candidate.get('existing_uid') or candidate['existing_uid']==previous['uid'], '中断恢复 UID 冲突'
                        assert previous.get('metadata_verified') is True, '中断回执缺少已核验元数据'
                        assert all(previous.get(k)==event.get(k) for k in ('title','notes','start','end','all_day')), '中断回执与当前方案不一致'
                        assert previous.get('location','')==event.get('location',''), '中断回执地点与当前方案不一致'
                        extra_plan={**event,'uid':previous['uid'],'calendar':CALENDAR_NAME,
                                    'start_timestamp':datetime.fromisoformat(previous['start_utc'].replace('Z','+00:00')).timestamp(),
                                    'end_timestamp':datetime.fromisoformat(previous['end_utc'].replace('Z','+00:00')).timestamp()}
                        extra_file=Path(temp)/'recovery-inspect.json';extra_file.write_text(json.dumps(extra_plan,ensure_ascii=False))
                        error_kind='inspection_transport_failed'
                        result=run_attachment_helper('review-inspect',extra_file)
                        if result.returncode:
                            helper_code=result.returncode
                            error_kind={66:'source_pdf_unreadable',70:'calendar_metadata_changed',71:'attachment_inspection_unsupported',
                                        74:'alarm_unverified',75:'attachment_content_unverified',77:'calendar_permission_unavailable',
                                        80:'duplicate_attachment_guard'}.get(helper_code,'inspection_failed')
                            raise RuntimeError('已有日程只读核验未通过：'+error_kind)
                        error_kind='invalid_inspection_receipt'
                        extra=json.loads(result.stdout)
                        assert extra.get('uid')==previous['uid'] and extra.get('event_key')==event['event_key'], '附件回执对象不符'
                        verify_extras_receipt(event,extra)
                    except Exception as exc:
                        if isinstance(exc,subprocess.TimeoutExpired):error_kind='inspection_timeout'
                        failure={'stage':'review-inspect','kind':error_kind,'error_type':type(exc).__name__,'checked_at':datetime.now().isoformat()}
                        if helper_code is not None:failure['helper_exit_code']=helper_code
                        with transaction() as state:state[bucket][docid].setdefault('calendar_recovery_errors',{})[event['event_key']]=failure
                        raise
                    rec={**previous,**extra,'verified':True};receipts[event['event_key']]=rec
                    with transaction() as state:
                        current=state[bucket][docid]
                        current.setdefault(partial_key,{})[event['event_key']]=rec
                        current.get('calendar_recovery_errors',{}).pop(event['event_key'],None)
                    continue
                planned_events.append(candidate)
            if planned_events:
                payload={'mode':'apply','events':planned_events,'calendar_name':CALENDAR_NAME}
                if fast_lookup:
                    # This top-level hint is generated under calendar_review.lock.
                    # Never promote a model-supplied hint from a decision/event.
                    snapshot=calendar_marker_snapshot(planned_events)
                    if snapshot is not None:payload['trusted_calendar_lookup']=snapshot
                plan.write_text(json.dumps(payload,ensure_ascii=False))
                result=subprocess.run(['/usr/bin/osascript','-l','JavaScript',str(Path(__file__).with_name('court_review_calendar.js')),str(plan),str(receipt)],capture_output=True,text=True,timeout=300)
                if receipt.exists():
                    receipts.update(json.loads(receipt.read_text()))
                    with transaction() as state:state[bucket][docid][partial_key]=receipts
                if result.returncode:raise RuntimeError('日历写入或核验失败：'+result.stderr[:500])
            for event in planned_events:
                rec=receipts[event['event_key']]
                assert rec.get('metadata_verified') is True and rec.get('uid'), '文本日程尚未回读'
                extra_plan={**event,'uid':rec['uid'],'calendar':CALENDAR_NAME,
                            'start_timestamp':datetime.fromisoformat(rec['start_utc'].replace('Z','+00:00')).timestamp(),
                            'end_timestamp':datetime.fromisoformat(rec['end_utc'].replace('Z','+00:00')).timestamp()}
                extra_file=Path(temp)/'extras.json';extra_file.write_text(json.dumps(extra_plan,ensure_ascii=False))
                result=run_attachment_helper('review-sync',extra_file)
                if result.returncode:raise RuntimeError('PDF附件或提前提醒未完成核验：'+result.stderr[:300])
                extra=json.loads(result.stdout)
                assert extra.get('uid')==rec['uid'] and extra.get('event_key')==event['event_key'], '附件回执对象不符'
                verify_extras_receipt(event,extra)
                rec.update(extra);rec['verified']=True
                atomic(receipt,receipts)
                with transaction() as state:state[bucket][docid][partial_key]=receipts
            record_receipts(docid,receipts,receipt_only=receipt_only,receipt_group=_receipt_group)
            return receipts



def receipt_snapshot(rec):
    fields=('uid','title','notes','start_utc','end_utc','location','all_day')
    assert all(field in rec for field in fields), '缺少日程原始快照'
    return {key:rec[key] for key in fields}



def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='command',required=True)
    sub.add_parser('status');p=sub.add_parser('apply');p.add_argument('id');p=sub.add_parser('pending');p.add_argument('--limit',type=int,default=20)
    p=sub.add_parser('context');p.add_argument('case_number')
    p=sub.add_parser('register-ocr');p.add_argument('id');p.add_argument('metadata')
    p=sub.add_parser('review');p.add_argument('id');p.add_argument('decision')
    p=sub.add_parser('receipts');p.add_argument('id');p.add_argument('receipts')
    args=ap.parse_args()
    if args.command=='apply':print(json.dumps(apply_calendar(args.id),ensure_ascii=False));return
    if args.command=='register-ocr':register_ocr(args.id,**json.loads(Path(args.metadata).read_text()));return
    if args.command=='review':record_review(args.id,json.loads(Path(args.decision).read_text()));return
    if args.command=='receipts':record_receipts(args.id,json.loads(Path(args.receipts).read_text()));return
    data=json.loads(LEDGER.read_text()) if LEDGER.exists() else {'documents':{},'cases':{},'events':{}}
    docs=list(data['documents'].values())
    if args.command=='status':
        from collections import Counter
        print(json.dumps({'documents':len(docs),'ocr':dict(Counter(d['ocr_status'] for d in docs)),'review':dict(Counter(d['review_status'] for d in docs)),'refresh':sum(d.get('context_refresh_needed',False) for d in docs)},ensure_ascii=False))
    elif args.command=='pending':print(json.dumps([d for d in docs if d['review_status'] in ('pending','ready') or d.get('context_refresh_needed')][:args.limit],ensure_ascii=False,indent=2))
    elif args.command=='context':
        case=canonical_case(args.case_number);assert case
        print(json.dumps({'case_number':case,'documents':[d for d in docs if d['case_number']==case],'events':{k:v for k,v in data['events'].items() if k.startswith(case+'|')}},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
