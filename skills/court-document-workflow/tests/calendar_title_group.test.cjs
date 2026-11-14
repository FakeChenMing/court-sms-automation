const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const context={ObjC:{import(){}}};vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../scripts/runtime/court_calendar_titles.js'),'utf8'),context);
const case_number='（2026）测01民初99999号';
const p={case_number,title:case_number+'｜甲公司等诉乙公司等｜开庭（带原件）',
 full_parties:['甲公司','甲某','乙公司','乙某'],parties:['甲公司','甲某','乙公司','乙某'],
 party_sides:[{role:'原告',names:['甲公司','甲某']},{role:'被告',names:['乙公司','乙某']}],
 display_parties:['甲公司等','乙公司等']};
const result=context.titlePartyAudit(p);
assert.deepEqual(Array.from(result.full_parties),p.full_parties);
assert.deepEqual(Array.from(result.display_parties),p.display_parties);
for(const altered of [
 {...p,full_parties:['甲公司','乙公司']},
 {...p,party_sides:[{role:'原告',names:['甲公司']},{role:'被告',names:['乙公司','乙某']}]},
 {...p,display_parties:['甲某等','乙公司等']},
 {...p,title:case_number+'｜甲公司等诉未确认公司等｜开庭'},
])assert.throws(()=>context.titlePartyAudit(altered),/主体|首名/);
assert.doesNotThrow(()=>context.titlePartyAudit({parties:['甲公司','乙公司'],title:case_number+'甲公司诉乙公司开庭'}));
assert.throws(()=>context.titlePartyAudit({parties:['甲公司','甲某','乙公司'],title:case_number+'甲公司等诉乙公司开庭'}),/主体/);
console.log('calendar title abbreviation audit tests passed');
