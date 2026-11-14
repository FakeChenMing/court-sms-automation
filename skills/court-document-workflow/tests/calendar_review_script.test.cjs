// Calendar script against an in-memory application: never opens Calendar.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../scripts/runtime/court_review_calendar.js'),'utf8');
function harness(plan,existing){
 const rows=existing||[],newRows=[],hooks={};
 let propertyReads=0,columnReads=0,whereReads=0,elementQueries=0;
 const make=(fields)=>{
  const vals={summary:'',startDate:new Date(),endDate:new Date(),description:'',location:'',alldayEvent:false,...fields};
  const uid=fields.uid||'new-uid';
  let proxy;proxy=new Proxy({}, {get:(_,k)=>k==='uid'?()=>uid:k==='properties'?()=>{propertyReads++;return {...vals,uid};}:k==='delete'?()=>{if(hooks.beforeDelete)hooks.beforeDelete(uid);const i=rows.indexOf(proxy);if(i>=0)rows.splice(i,1);}:()=>vals[k],set:(_,k,v)=>{vals[k]=v;return true;}});return proxy;
 };
 const events={whose:criteria=>{whereReads++;const selected=()=>rows.filter(e=>criteria.uid?e.uid()===criteria.uid:(e.startDate()>criteria.startDate._greaterThan&&e.startDate()<criteria.startDate._lessThan));const query=()=>{elementQueries++;return selected();};for(const property of ['uid','description'])query[property]=()=>{columnReads++;return selected().map(e=>e[property]());};return query;},byId:uid=>{if(hooks.byId)return hooks.byId(uid);throw new Error('Invalid index (-1719): newly saved byId is not ready');},push:e=>{rows.push(e);newRows.push(e);if(hooks.afterPush)hooks.afterPush(e);}};
 for(const property of ['uid','summary','startDate','endDate','description','location','alldayEvent'])events[property]=()=>{columnReads++;return rows.map(e=>e[property]());};
 const calendar={exists:()=>true,events};
 let writes=[];
 const NS={stringWithContentsOfFileEncodingError:()=>JSON.stringify(plan),alloc:{initWithUTF8String:x=>({writeToFileAtomicallyEncodingError:()=>writes.push(JSON.parse(x))})}};
 const ctx={ObjC:{import(){},unwrap:x=>x},$:{NSString:NS,NSUTF8StringEncoding:0},Application:()=>({calendars:{byName:()=>calendar},Event:make}),Date,JSON,Error};
 vm.createContext(ctx);vm.runInContext(source,ctx);
 return {make,run:()=>JSON.parse(ctx.run(['plan','receipt'])),rows,newRows,writes,hooks,counts:()=>({columnReads,whereReads}),performance:()=>({propertyReads,elementQueries})};
}
const event={existing_uid:'old-uid',metadata_only:true,basis_type:'internal_check',event_key:'（2026）测01民初1号|待核|送达',case_number:'（2026）测01民初1号',title:'旧事项待核',notes:'当事人：甲乙\n法院材料事项ID：（2026）测01民初1号|待核|送达',start:'2020-01-01T00:00:00+08:00',end:'2020-01-02T00:00:00+08:00',all_day:true,location:'原地点'};
let h=harness({mode:'apply',events:[event]});
h.rows.push(h.make({uid:'old-uid',summary:'旧标题',description:'案号：（2026）测01民初1号',startDate:new Date(event.start),endDate:new Date(event.end),alldayEvent:true,location:'原地点'}));
let result=h.run();assert.equal(h.newRows.length,0);assert.equal(h.rows[0].startDate().toISOString(),new Date(event.start).toISOString());assert.equal(h.rows[0].location(),'原地点');assert.equal(result[event.event_key].metadata_verified,true);assert.equal(result[event.event_key].verified,false);
assert.deepEqual(h.performance(),{propertyReads:2,elementQueries:2});
let wrong={...event,location:'改后地点'};h=harness({mode:'apply',events:[wrong]});h.rows.push(h.make({uid:'old-uid',summary:'旧标题',description:'案号：（2026）测01民初1号',startDate:new Date(event.start),endDate:new Date(event.end),alldayEvent:true,location:'原地点'}));assert.throws(()=>h.run(),/不得改变日期/);
h=harness({mode:'apply',events:[{...event,existing_uid:undefined,metadata_only:false}]});assert.throws(()=>h.run(),/待核事项/);assert.equal(h.rows.length,0);
h=harness({mode:'apply',events:[{...event,basis_type:'document_explicit',existing_uid:undefined,metadata_only:false}]});assert.throws(()=>h.run(),/过去/);assert.equal(h.rows.length,0);
h=harness({mode:'apply',events:[event]});assert.throws(()=>h.run(),/已有日程/);assert.equal(h.rows.length,0);
const sha='a'.repeat(64),receiptKey=event.case_number+'|收到材料|2026-01-02';
const proof={source_rowid:1,received_at:'2026-01-02T12:00:00+08:00',kind:'delivery_sms',source_doc:'document-1',document_sha256:sha,binding_kind:'download_manifest',binding_source:'fixture/download_manifest.json#rowid=1'};
const receipt={...event,existing_uid:undefined,metadata_only:false,event_kind:'receipt',basis_type:'document_received',event_key:receiptKey,title:event.case_number+' 甲与乙 收到举证通知书',notes:'当事人：甲乙\n法院材料事项ID：'+receiptKey,start:'2026-01-02T00:00:00+08:00',end:'2026-01-03T00:00:00+08:00',received_date:'2026-01-02',received_evidence:proof,receipt_documents:[{id:'document-1',sha256:sha,source_path:'/fixture/doc1.pdf',binding:proof}],attachment_manifest:[{source_path:'/fixture/doc1.pdf',sha256:sha}],due_dates:[],pending_deadlines:['举证到期尚待核实']};
h=harness({mode:'apply',events:[receipt]});result=h.run();assert.equal(h.newRows.length,1);assert.equal(result[receiptKey].metadata_verified,true);h.run();assert.equal(h.newRows.length,1);assert.equal(h.rows.length,1);
h=harness({mode:'apply',events:[{...receipt,received_date:'2026-01-03'}]});assert.throws(()=>h.run(),/收到日期/);assert.equal(h.rows.length,0);
h=harness({mode:'apply',events:[{...receipt,event_key:'unstable'}]});assert.throws(()=>h.run(),/稳定标识/);
h=harness({mode:'apply',events:[{...receipt,due_dates:['2026-01-03']}]});assert.throws(()=>h.run(),/到期事项/);
h=harness({mode:'apply',events:[{...receipt,received_evidence:{...proof,binding_kind:'case_sms_week'}}]});assert.throws(()=>h.run(),/精确下载/);
const deadline={...receipt,event_kind:'deadline',basis_type:'document_explicit',event_key:event.case_number+'|举证|提交证据',is_deadline:true,alarm_minutes:-10080,retrospective_pair:true,paired_receipt_key:receiptKey,due_date:'2026-02-01',start:'2026-02-01T00:00:00+08:00',end:'2026-02-02T00:00:00+08:00'};
deadline.notes='当事人：甲乙\n法院材料事项ID：'+deadline.event_key;
h=harness({mode:'apply',events:[deadline]});h.run();assert.equal(h.newRows.length,1);
h=harness({mode:'apply',events:[{...deadline,retrospective_pair:undefined}]});assert.throws(()=>h.run(),/过去/);assert.equal(h.rows.length,0);
h=harness({mode:'apply',events:[{...deadline,paired_receipt_key:'other'}]});assert.throws(()=>h.run(),/关联无效/);
h=harness({mode:'apply',events:[{...deadline,alarm_minutes:undefined}]});assert.throws(()=>h.run(),/7日提醒/);
h=harness({mode:'apply',events:[{...event,event_kind:'appointment',basis_type:'document_explicit',existing_uid:undefined,metadata_only:false,retrospective_pair:true,paired_receipt_key:receiptKey,due_date:'2020-01-01'}]});assert.throws(()=>h.run(),/过去/);
h=harness({mode:'read',from:'2026-01-01T00:00:00+08:00',to:'2027-01-01T00:00:00+08:00',case_number:event.case_number});
for(const [uid,start,notes] of [['included','2026-01-01T00:00:00+08:00',event.case_number],['excluded-old','2025-12-31T23:59:59+08:00',event.case_number],['excluded-next','2027-01-01T00:00:00+08:00',event.case_number],['excluded-case','2026-01-02T00:00:00+08:00','（2026）测01民初2号']])h.rows.push(h.make({uid,startDate:new Date(start),endDate:new Date(new Date(start).valueOf()+3600000),description:notes}));
result=h.run();assert.deepEqual(result.map(x=>x.uid),['included']);assert.deepEqual(h.counts(),{columnReads:7,whereReads:0});assert.equal(h.newRows.length,0);
h=harness({mode:'read',uids:['selected']});h.rows.push(h.make({uid:'other'}),h.make({uid:'selected'}));assert.deepEqual(h.run().map(x=>x.uid),['selected']);assert.deepEqual(h.counts(),{columnReads:7,whereReads:0});
// Large date-window lookups must stay columnar; only the saved event uses properties().
h=harness({mode:'apply',events:[receipt]});
for(let i=0;i<200;i++)h.rows.push(h.make({uid:'unrelated-'+i,startDate:new Date(receipt.start),endDate:new Date(receipt.end),description:'其他事项 '+i}));
h.run();assert.deepEqual(h.performance(),{propertyReads:1,elementQueries:1});assert.equal(h.newRows.length,1);
// Matching duplicate markers still block mutation before save.
h=harness({mode:'apply',events:[receipt]});for(const uid of ['duplicate-1','duplicate-2'])h.rows.push(h.make({uid,startDate:new Date(receipt.start),endDate:new Date(receipt.end),description:receipt.notes}));assert.throws(()=>h.run(),/多个匹配/);assert.equal(h.newRows.length,0);
// Title and note compare-before-write guards continue to preserve manual edits.
for(const [field,value,match] of [['expected_previous_title','旧标题',/标题在审查后/],['expected_previous_notes','原备注',/备注已变化/]]){
 h=harness({mode:'apply',events:[{...event,[field]:value}]});h.rows.push(h.make({uid:'old-uid',summary:'用户改过的标题',description:'案号：'+event.case_number+'\n用户补充备注',startDate:new Date(event.start),endDate:new Date(event.end),location:event.location,alldayEvent:true}));assert.throws(()=>h.run(),match);assert.equal(h.rows[0].summary(),'用户改过的标题');assert.equal(h.writes.length,0);
}
// A second marker appearing after save must fail the final duplicate-count readback.
h=harness({mode:'apply',events:[receipt]});h.hooks.afterPush=e=>h.rows.push(h.make({uid:'concurrent-duplicate',startDate:new Date(receipt.start),endDate:new Date(receipt.end),description:receipt.notes}));assert.throws(()=>h.run(),/回读校验不一致/);assert.equal(h.writes.length,0);
const lookup=rows=>({schema_version:1,calendar_name:'法院工作日历',by_event_key:{[receipt.event_key]:rows}});
h=harness({mode:'apply',events:[receipt],trusted_calendar_lookup:lookup([])});h.run();assert.deepEqual(h.counts(),{columnReads:2,whereReads:2});assert.equal(h.newRows.length,1);
h=harness({mode:'apply',events:[receipt],trusted_calendar_lookup:lookup([{uid:'one',title:'题1',notes:receipt.notes},{uid:'two',title:'题2',notes:receipt.notes}])});assert.throws(()=>h.run(),/多个匹配/);assert.equal(h.newRows.length,0);
h=harness({mode:'apply',events:[receipt],trusted_calendar_lookup:lookup([{uid:'kept-uid',title:receipt.title,notes:receipt.notes}])});h.rows.push(h.make({uid:'kept-uid',summary:receipt.title,description:receipt.notes+'\n人工新增备注',startDate:new Date(receipt.start),endDate:new Date(receipt.end),location:receipt.location,alldayEvent:true}));assert.throws(()=>h.run(),/标题或备注已变化/);assert.equal(h.newRows.length,0);assert(h.rows[0].description().includes('人工新增备注'));
h=harness({mode:'apply',events:[receipt],trusted_calendar_lookup:lookup([])});h.hooks.afterPush=()=>h.rows.push(h.make({uid:'concurrent-new',startDate:new Date(receipt.start),endDate:new Date(receipt.end),description:receipt.notes}));assert.throws(()=>h.run(),/回读校验不一致/);assert.equal(h.writes.length,0);
h=harness({mode:'apply',events:[receipt],trusted_calendar_lookup:{schema_version:9}});h.run();assert.deepEqual(h.counts(),{columnReads:4,whereReads:3});
// A successful byId property read avoids the UID query and is reused only once.
h=harness({mode:'apply',events:[receipt]});h.hooks.byId=uid=>h.rows.find(e=>e.uid()===uid);h.run();assert.deepEqual(h.performance(),{propertyReads:1,elementQueries:0});assert.deepEqual(h.counts(),{columnReads:4,whereReads:2});
// Failure can occur on properties(), after obtaining a seemingly valid byId object.
h=harness({mode:'apply',events:[receipt]});let failedProperties=0;h.hooks.byId=()=>({properties(){failedProperties++;throw new Error('Invalid index (-1719)');}});h.run();assert.equal(failedProperties,1);assert.deepEqual(h.performance(),{propertyReads:1,elementQueries:1});
// An unexpected UID from byId is not accepted as the requested event.
h=harness({mode:'apply',events:[receipt]});h.hooks.byId=()=>({properties:()=>({uid:'wrong-uid'})});h.run();assert.deepEqual(h.performance(),{propertyReads:1,elementQueries:1});
const secondProof={...proof,source_doc:'document-2',document_sha256:'b'.repeat(64),source_rowid:2,received_at:'2026-01-02T13:00:00+08:00'};
const grouped={...receipt,receipt_documents:[...receipt.receipt_documents,{id:'document-2',sha256:secondProof.document_sha256,source_path:'/fixture/doc2.pdf',binding:secondProof}],attachment_manifest:[...receipt.attachment_manifest,{source_path:'/fixture/doc2.pdf',sha256:secondProof.document_sha256}]};
h=harness({mode:'apply',events:[grouped]});h.run();assert.equal(h.newRows.length,1);
h=harness({mode:'apply',events:[{...grouped,attachment_manifest:receipt.attachment_manifest}]});assert.throws(()=>h.run(),/某份原件PDF/);assert.equal(h.newRows.length,0);
h=harness({mode:'apply',events:[{...grouped,receipt_documents:[receipt.receipt_documents[0],{...grouped.receipt_documents[1],binding:{...secondProof,received_at:'2026-01-03T13:00:00+08:00'}}]}]});assert.throws(()=>h.run(),/收到日期/);
console.log('Isolated Calendar script behavior checks passed');
