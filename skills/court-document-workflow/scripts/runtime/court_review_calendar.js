ObjC.import('Foundation');
function run(argv) {
 const data=JSON.parse(ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(argv[0],$.NSUTF8StringEncoding,null)));
 const calendarName=data.calendar_name||'法院工作日历';
 const app=Application('com.apple.iCal');const cal=app.calendars.byName(calendarName);
 const propertyCache=new Map();
 if(!cal.exists())throw new Error('工作日历不存在');
 function norm(s){return (s||'').replace(/\s/g,'').replace(/\(/g,'（').replace(/\)/g,'）');}
 function checkedDate(value,label){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||isNaN(new Date(value+'T00:00:00+08:00').valueOf())||new Date(value+'T00:00:00Z').toISOString().slice(0,10)!==value)throw new Error(label+'无效');
  return value;
 }
 function validateBindingProof(proof,received){
  if(received<'2026-01-01'||!proof||proof.kind!=='delivery_sms'||!/^\d+$/.test(String(proof.source_rowid||''))||Number(proof.source_rowid)<=0||typeof proof.source_doc!=='string'||!proof.source_doc||!/^[a-f0-9]{64}$/.test(proof.document_sha256||''))throw new Error('缺少对应文书的已核对收件记录');
  if(!['download_manifest','download_job'].includes(proof.binding_kind)||typeof proof.binding_source!=='string'||!proof.binding_source.trim())throw new Error('收件记录缺少精确下载关联来源');
  let receivedAt=proof.received_at;
  if(typeof receivedAt!=='string'||!receivedAt)throw new Error('缺少收件时间');
  if(!/(Z|[+-]\d\d:\d\d)$/.test(receivedAt))receivedAt+='+08:00';
  const stamp=new Date(receivedAt);
  if(isNaN(stamp.valueOf())||stamp>new Date()||new Date(stamp.valueOf()+28800000).toISOString().slice(0,10)!==received)throw new Error('收到日期与记录不一致或位于未来');
 }
 function validateTemporal(p,start,end){
  const kind=p.event_kind||(p.is_deadline?'deadline':'appointment');
  if(!['receipt','deadline','appointment'].includes(kind))throw new Error('日程类型无效');
  if((p.basis_type==='document_received')!==(kind==='receipt'))throw new Error('收到节点与收件依据类型不一致');
  if(kind==='appointment')return false;
  const received=checkedDate(p.received_date,'收到日期');
  if(kind==='receipt'){
   if(!Array.isArray(p.receipt_documents)||!p.receipt_documents.length)throw new Error('收到群组缺少逐份文书清单');
   const seen=[];
   for(const member of p.receipt_documents){
    if(!member||typeof member.id!=='string'||!member.id||seen.includes(member.id)||!member.binding||member.binding.source_doc!==member.id||member.binding.document_sha256!==member.sha256||member.case_number&&member.case_number!==p.case_number)throw new Error('收到群组成员身份无效');
    validateBindingProof(member.binding,received);seen.push(member.id);
    if(!Array.isArray(p.attachment_manifest)||!p.attachment_manifest.some(x=>x.source_path===member.source_path&&x.sha256===member.sha256))throw new Error('收到群组缺少某份原件PDF附件');
   }
   if(p.received_evidence&&!p.receipt_documents.some(m=>['source_doc','document_sha256','source_rowid','received_at','kind','binding_kind','binding_source'].every(k=>m.binding[k]===p.received_evidence[k])))throw new Error('群组摘要缺少精确下载关联');
  }else validateBindingProof(p.received_evidence,received);
  if(!/\+08:00$/.test(p.start)||!/\+08:00$/.test(p.end))throw new Error('双节点日程必须使用北京时间');
  const receiptKey=p.case_number+'|收到材料|'+received;
  if(kind==='receipt'){
   if(p.event_key!==receiptKey||p.all_day!==true||!p.start.startsWith(received+'T00:00:00')||![86399000,86400000].includes(end-start))throw new Error('收到节点须登记在真实收到当天并使用稳定标识');
   if(p.is_deadline||p.alarm_minutes!==undefined&&p.alarm_minutes!==null)throw new Error('收到节点不能冒充到期提醒');
   if(!Array.isArray(p.due_dates)||!Array.isArray(p.pending_deadlines)||p.pending_deadlines.some(x=>typeof x!=='string'||!x.trim()))throw new Error('收到节点须列明已知到期日和待核期限');
   const keys=[];
   for(const due of p.due_dates){
    if(!due||typeof due!=='object'||Array.isArray(due)||typeof due.subject!=='string'||!due.subject.trim()||typeof due.event_key!=='string'||!due.event_key||due.event_key===receiptKey||keys.includes(due.event_key))throw new Error('到期事项缺少名称或唯一关联标识');
    checkedDate(due.date,'到期日期');keys.push(due.event_key);
   }
   return true;
  }
  if(!['document_explicit','calculated'].includes(p.basis_type)||checkedDate(p.due_date,'到期日期')!==p.start.slice(0,10)||p.paired_receipt_key!==receiptKey)throw new Error('到期节点日期或收到节点关联无效');
  if(p.alarm_minutes!==-10080)throw new Error('到期事项须设置提前7日提醒');
  return p.retrospective_pair===true;
 }
 function read(e){const p=propertyCache.get(e)||e.properties();propertyCache.delete(e);return {uid:p.uid,title:p.summary,start_utc:p.startDate.toISOString(),end_utc:p.endDate.toISOString(),location:p.location||'',notes:p.description||'',all_day:p.alldayEvent};}
 function readColumns(){
  // Seven collection requests, rather than seven Apple Events per calendar row.
  // Filter locally so an event exactly at the requested range start is included.
  const events=cal.events,cols={uid:events.uid(),title:events.summary(),start:events.startDate(),end:events.endDate(),location:events.location(),notes:events.description(),all_day:events.alldayEvent()};
  const count=cols.uid.length;
  if(Object.keys(cols).some(k=>!Array.isArray(cols[k])||cols[k].length!==count))throw new Error('日历读取期间记录数量变化，请重试');
  const wanted=data.uids?new Set(data.uids):null,from=data.from?new Date(data.from).valueOf():null,to=data.to?new Date(data.to).valueOf():null;
  if(!wanted&&(!(to>from)||from===null||to===null))throw new Error('日历读取范围无效');
  const result=[];
  for(let i=0;i<count;i++){
   const start=cols.start[i];
   if(wanted?!wanted.has(cols.uid[i]):start.valueOf()<from||start.valueOf()>=to)continue;
   if(data.case_number&&!norm((cols.notes[i]||'')+' '+(cols.title[i]||'')).includes(norm(data.case_number)))continue;
   result.push({uid:cols.uid[i],title:cols.title[i],start_utc:start.toISOString(),end_utc:cols.end[i].toISOString(),location:cols.location[i]||'',notes:cols.notes[i]||'',all_day:cols.all_day[i]});
  }
  return result;
 }
 // A newly saved UID may not resolve through byId yet. Read properties inside
 // the try, verify identity, and fall back to the proven query object on failure.
 // Cache only that just-read snapshot; each subsequent byUid call reads afresh.
 function byUid(uid){
  try{const e=cal.events.byId(uid),p=e.properties();if(p.uid===uid){propertyCache.set(e,p);return [e];}}catch(error){}
  return cal.events.whose({uid:uid})();
 }
 function rangeMatches(start,end,marker){
  const selected=cal.events.whose({startDate:{_greaterThan:new Date(start),_lessThan:new Date(end)}});
  const uids=selected.uid(),notes=selected.description();
  if(!Array.isArray(uids)||!Array.isArray(notes)||uids.length!==notes.length)throw new Error('日历查重期间记录数量变化，请重试');
  return uids.filter((uid,i)=>(notes[i]||'').includes(marker));
 }
 function prelookup(p,marker){
  // Only the Python writer emits this top-level hint; old callers omit it.
  // It accelerates lookup only. The post-save UID and range readback is unchanged.
  const snapshot=data.trusted_calendar_lookup;
  if(!snapshot||snapshot.schema_version!==1||snapshot.calendar_name!==calendarName||!snapshot.by_event_key||!Object.prototype.hasOwnProperty.call(snapshot.by_event_key,p.event_key))return null;
  const matches=snapshot.by_event_key[p.event_key];
  if(!Array.isArray(matches)||matches.some(row=>!row||typeof row.uid!=='string'||!row.uid||typeof row.title!=='string'||typeof row.notes!=='string'||!row.notes.split(/\r?\n/).includes(marker)))return null;
  return matches;
 }
 if(data.mode==='read')return JSON.stringify(readColumns());
 if(data.mode!=='apply')throw new Error('unsupported mode');
 const receipts={};
 for(const p of data.events){
  const start=new Date(p.start),end=new Date(p.end),marker='法院材料事项ID：'+p.event_key;
  if(!(end>start)||isNaN(start.valueOf()))throw new Error('无效起止时间');
  const today=new Date();today.setHours(0,0,0,0);
  if(p.basis_type==='internal_check'&&!(p.metadata_only&&p.existing_uid))throw new Error('待核事项不写入日期日历');
  if(p.metadata_only&&!p.existing_uid)throw new Error('内容修复必须指定已有UID');
  const historicalNode=validateTemporal(p,start,end);
  if(start<(p.all_day?today:new Date())&&!(p.metadata_only&&p.existing_uid)&&!historicalNode)throw new Error('禁止补建过去安排；仅授权收到/已核实双节点补齐');
  if(!p.notes.includes(marker)||!norm(p.notes).includes(norm(p.case_number)))throw new Error('缺少事项和案号标识');
  let candidates,beforeSnapshot;
  if(p.existing_uid){
   candidates=byUid(p.existing_uid);
   if(candidates.length!==1)throw new Error('已有日程与案号不匹配');
   beforeSnapshot=read(candidates[0]);
   if(!norm(beforeSnapshot.notes).includes(norm(p.case_number)))throw new Error('已有日程与案号不匹配');
   if(p.expected_previous_title!==undefined&&beforeSnapshot.title!==p.expected_previous_title&&beforeSnapshot.title!==p.title)throw new Error('日程标题在审查后已变化，须重新核对以保留人工内容');
   if(p.expected_previous_notes!==undefined&&beforeSnapshot.notes!==p.expected_previous_notes)throw new Error('中断后备注已变化，须重新审查以保留人工内容');
   if(new Date(beforeSnapshot.start_utc).valueOf()!==start.valueOf()&&!p.supersession_verified)throw new Error('排期变更关系未核实');
  }else{
   const snapshot=prelookup(p,marker);
   if(snapshot!==null){
    if(snapshot.length>1)throw new Error('已有多个匹配日程，需核对');
    candidates=snapshot.flatMap(row=>byUid(row.uid));
    if(candidates.length!==snapshot.length){
     candidates=rangeMatches(new Date(start.valueOf()-86400000).toISOString(),new Date(end.valueOf()+86400000).toISOString(),marker).flatMap(uid=>byUid(uid));
    }else if(snapshot.length){
     beforeSnapshot=read(candidates[0]);
     if(beforeSnapshot.title!==snapshot[0].title||beforeSnapshot.notes!==snapshot[0].notes)throw new Error('查重后标题或备注已变化，须重新审查以保留人工内容');
     if(new Date(beforeSnapshot.start_utc).valueOf()!==start.valueOf()&&!p.supersession_verified)throw new Error('排期变更关系未核实');
    }
   }else{
    candidates=rangeMatches(new Date(start.valueOf()-86400000).toISOString(),new Date(end.valueOf()+86400000).toISOString(),marker).flatMap(uid=>byUid(uid));
   }
  }
  if(candidates.length>1)throw new Error('已有多个匹配日程，需核对');
  let e=candidates[0];
  if(p.metadata_only){
   if(!e)throw new Error('内容修复不得新建事件');
   const old=beforeSnapshot||read(e),endDifference=new Date(old.end_utc).valueOf()-end.valueOf();
   if(new Date(old.start_utc).valueOf()!==start.valueOf()||!(endDifference===0||(p.all_day&&endDifference===-1000))||old.location!==(p.location||'')||old.all_day!==!!p.all_day)throw new Error('内容修复不得改变日期、地点或全天状态');
  }
  if(!e){e=app.Event({summary:p.title,startDate:start,endDate:end,description:p.notes,location:p.location||'',alldayEvent:!!p.all_day});cal.events.push(e);}
  else if(p.metadata_only){e.summary=p.title;e.description=p.notes;}
  else{e.alldayEvent=false;e.summary=p.title;e.startDate=start;e.endDate=end;e.description=p.notes;e.location=p.location||'';e.alldayEvent=!!p.all_day;}
  const uid=e.uid();
  // Fresh lookup after save, not the in-memory object alone.
  const saved=byUid(uid);
  if(saved.length!==1)throw new Error('无法回读日程');
  const actual=read(saved[0]);
  const matches=rangeMatches(new Date(start.valueOf()-86400000).toISOString(),new Date(end.valueOf()+86400000).toISOString(),marker);
  if(actual.title!==p.title||new Date(actual.start_utc).valueOf()!==start.valueOf()||!(new Date(actual.end_utc).valueOf()===end.valueOf()||(p.all_day&&new Date(actual.end_utc).valueOf()+1000===end.valueOf()))||actual.notes!==p.notes||actual.location!==(p.location||'')||actual.all_day!==!!p.all_day||matches.length!==1)throw new Error('回读校验不一致 '+JSON.stringify({start:actual.start_utc,end:actual.end_utc,all_day:actual.all_day,expected_start:p.start,expected_end:p.end,title_ok:actual.title===p.title,notes_ok:actual.notes===p.notes,location_ok:actual.location===(p.location||''),matches:matches.length}));
  receipts[p.event_key]=Object.assign(actual,{verified:false,metadata_verified:true,start:p.start,end:p.end,match_count:matches.length});
  if(argv[1])$.NSString.alloc.initWithUTF8String(JSON.stringify(receipts)).writeToFileAtomicallyEncodingError(argv[1],true,$.NSUTF8StringEncoding,null);
 }
 return JSON.stringify(receipts);
}
