ObjC.import('Foundation');
// Presentation can be abbreviated; the full verified roster remains auditable.
function titlePartyAudit(p){
 const unique=a=>Array.from(new Set(a));
 const equal=(a,b)=>a.length===b.length&&a.every(x=>b.includes(x));
 if(!p.party_sides){
  if(!Array.isArray(p.parties)||!p.parties.length||!p.parties.every(name=>p.title.includes(name)))throw new Error('标题缺少已确认主体');
  return {full_parties:unique(p.parties),display_parties:unique(p.parties)};
 }
 if(!Array.isArray(p.full_parties)||!Array.isArray(p.party_sides)||!p.party_sides.length)throw new Error('标题简写缺完整主体证据');
 const roles={},short=names=>names[0]+(names.length>1?'等':'');
 for(const side of p.party_sides){
  if(!side.role||!Array.isArray(side.names)||!side.names.length||side.names.some(n=>typeof n!=='string'||!n.trim()))throw new Error('标题分侧信息无效');
  roles[side.role]=unique((roles[side.role]||[]).concat(side.names));
 }
 const full=unique(Object.keys(roles).reduce((all,role)=>all.concat(roles[role]),[]));
 if(!equal(full,unique(p.full_parties))||(p.parties&&!equal(full,unique(p.parties))))throw new Error('标题简写遗漏完整主体名单');
 let label='',display=[];
 for(const pair of [['原告','被告','诉'],['上诉人','被上诉人','与'],['申请人','被申请人','与'],['申请方','被申请方','与'],['申请执行人','被执行人','与']]){
  if(roles[pair[0]]&&roles[pair[1]]){
   display=[short(roles[pair[0]]),short(roles[pair[1]])];label=display.join(pair[2]);
   const others=full.filter(n=>!roles[pair[0]].concat(roles[pair[1]]).includes(n));
   if(others.length){display.push(short(others));label+='；另涉'+short(others);}break;
  }
 }
 if(!label){
  const oneSide=full.length===1||(Object.keys(roles).length===1&&['原告','被告','被传唤人','上诉人','被上诉人'].includes(Object.keys(roles)[0]));
  label=oneSide?short(full)+'（另一方未确认）':full.slice(0,2).join('与')+(full.length>2?'等':'');
  display=oneSide?[short(full)]:full.slice(0,2).map((n,i)=>n+(i===1&&full.length>2?'等':''));
 }
 if(!Array.isArray(p.display_parties)||JSON.stringify(p.display_parties)!==JSON.stringify(display)||!p.title.includes(label))throw new Error('标题简写与已确认分侧首名不一致');
 return {full_parties:full,display_parties:display,party_sides:p.party_sides};
}
// Metadata repair only. This entry point never creates or deletes an event.
function run(argv) {
 const plan=JSON.parse(ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(argv[0],$.NSUTF8StringEncoding,null)));
 const calendar=Application('com.apple.iCal').calendars.byName(plan.calendar_name||'法院工作日历');
 if(!calendar.exists()||!['read','apply'].includes(plan.mode))throw new Error('无效标题修复计划');
 function read(uid){
  const p=calendar.events.byId(uid).properties();
  return {uid:p.uid,title:p.summary,start_utc:p.startDate.toISOString(),end_utc:p.endDate.toISOString(),notes:p.description||'',location:p.location||'',all_day:p.alldayEvent};
 }
 function same(actual,expected,includeTitle){
  return ['uid','start_utc','end_utc','notes','location','all_day'].concat(includeTitle?['title']:[]).every(k=>actual[k]===expected[k]);
 }
 function norm(s){return (s||'').replace(/\s/g,'').replace(/\(/g,'（').replace(/\)/g,'）');}
 if(plan.mode==='read')return JSON.stringify(plan.uids.map(read));
 if(!Array.isArray(plan.events)||new Set(plan.events.map(x=>x.before.uid)).size!==plan.events.length)throw new Error('重复或无效日程');
 for(const p of plan.events){
  titlePartyAudit(p);
  if(!p.title.includes(p.case_number))throw new Error('标题缺少案号');
  if(!norm(p.before.notes+' '+p.before.title).includes(norm(p.case_number)))throw new Error('原日程完整案号不匹配');
  if(!same(read(p.before.uid),p.before,true))throw new Error('日程在核对后已变化：'+p.case_number);
 }
 const receipts=[];
 for(const p of plan.events){
  // Recheck immediately before the one permitted setter.
  const before=read(p.before.uid);
  if(!same(before,p.before,true))throw new Error('日程已被修改，保留现场：'+p.case_number);
  calendar.events.byId(p.before.uid).summary=p.title;
  const after=read(p.before.uid);
  if(after.title!==p.title||!same(after,before,false))throw new Error('标题写后回读不一致：'+p.case_number);
  receipts.push({...after,verified:true,title_only:true,...titlePartyAudit(p)});
  $.NSString.alloc.initWithUTF8String(JSON.stringify(receipts)).writeToFileAtomicallyEncodingError(argv[1],true,$.NSUTF8StringEncoding,null);
 }
 return JSON.stringify({verified:receipts.length});
}
