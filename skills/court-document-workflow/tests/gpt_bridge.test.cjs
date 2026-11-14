// Isolated worker contract tests. No CLI launch, credentials, real MCP, or Calendar.
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{EventEmitter}=require('events');
const bridge=require('../scripts/gpt_bridge.cjs');
const tool={name:'mcp_law__search_law',description:'synthetic read-only lookup',input_schema:{type:'object',properties:{q:{type:'string'}},required:['q']}};
let checks=0;
const requestModel=(request,options)=>bridge.requestModel(request,{versionRunner:()=>"codex-cli 0.167.0-alpha.4.1\n",...options});
const complete=[{type:'thread.started',thread_id:'synthetic'},{type:'item.completed',item:{type:'error',message:'Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable features.code_mode_host.'}},{type:'turn.started'},{type:'item.completed',item:{type:'agent_message',text:'synthetic'}},{type:'turn.completed',usage:{input_tokens:20,output_tokens:8}}].map(JSON.stringify).join('\n');
function fakeSpawn(envelope,{raw=complete,stderr='',code=0,hang=false}={}){
 return (binary,args,options)=>{
  assert.equal(binary,bridge.CLI);assert.notEqual(options.detached,true);
  assert(!options.env.OPENAI_API_KEY&&!options.env.CODEX_API_KEY&&!options.env.NODE_OPTIONS);
  const root=options.cwd;fakeSpawn.lastRoot=root;fakeSpawn.lastArgs=args;
  const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();child.kill=signal=>{fakeSpawn.lastSignal=signal;queueMicrotask(()=>child.emit('close',null));return true;};
  child.stdin.end=prompt=>{
   fakeSpawn.lastPrompt=prompt;if(hang)return;
   queueMicrotask(()=>{
    if(envelope!==undefined)fs.writeFileSync(path.join(root,'answer.json'),JSON.stringify(envelope));
    for(const chunk of (Array.isArray(stderr)?stderr:[stderr]))if(chunk)child.stderr.emit('data',Buffer.from(chunk));
    child.stdout.emit('data',Buffer.from(raw));child.emit('close',code);
   });
  };
  return child;
 };
}
async function main(){
 assert.equal(bridge.TIMEOUT_MS,1800000);checks++;
 const request={system:'Fixture review only',messages:[{role:'user',content:'synthetic source'}],tools:[tool],model:'wrong',output_config:{effort:'low'}};
 const result=await requestModel(request,{spawnImpl:fakeSpawn({kind:'final',tool_calls:[],final_text:'{"ok":true}'})});
 assert.equal(result.type,'message');assert.equal(result.model,'gpt-6.1-sol');assert.equal(result.runtime.reasoning_effort,'max');assert.equal(result.stop_reason,'end_turn');assert.deepEqual(JSON.parse(result.content[0].text),{ok:true});
 assert(!fs.existsSync(fakeSpawn.lastRoot));assert(fakeSpawn.lastArgs.includes('model_reasoning_effort="max"'));assert(!fakeSpawn.lastArgs.includes('low'));checks++;
 for(const flag of ['--ignore-user-config','--ephemeral','--strict-config','--sandbox','read-only','approval_policy="never"','features.skip_host_skill_discovery=true','tools.update_plan.enabled=false','tools.experimental_request_user_input.enabled=false'])assert(fakeSpawn.lastArgs.includes(flag));
 for(const feature of ['shell_tool','apps','browser_use','computer_use','plugins','code_mode_host','view_image','multi_agent','hooks','sleep_tool'])assert(bridge.DISABLED.includes(feature));checks++;
 const called=await requestModel(request,{spawnImpl:fakeSpawn({kind:'tool_calls',tool_calls:[{name:tool.name,input_json:'{"q":"fixture"}'}],final_text:''})});
 assert.equal(called.stop_reason,'tool_use');assert.equal(called.content[0].type,'tool_use');assert.equal(called.content[0].name,tool.name);assert.deepEqual(called.content[0].input,{q:'fixture'});assert.match(called.content[0].id,/^gpt_[0-9a-f]{32}$/);checks++;
 request.messages.push({role:'assistant',content:called.content},{role:'user',content:[{type:'tool_result',tool_use_id:called.content[0].id,content:'REAL_FIXTURE_RECEIPT'}]});
 await requestModel(request,{spawnImpl:fakeSpawn({kind:'final',tool_calls:[],final_text:'{"audit_used":true}'})});assert(fakeSpawn.lastPrompt.includes('REAL_FIXTURE_RECEIPT'));assert(fakeSpawn.lastPrompt.includes(called.content[0].id));checks++;
 for(const envelope of [
  {kind:'tool_calls',tool_calls:[{name:'exec_command',input_json:'{}'}],final_text:''},
  {kind:'tool_calls',tool_calls:[{name:tool.name,input_json:'[]'}],final_text:''},
  {kind:'tool_calls',tool_calls:[{name:tool.name,input_json:'not JSON'}],final_text:''},
  {kind:'final',tool_calls:[],final_text:'unstructured'},
  {kind:'final',tool_calls:[{name:tool.name,input_json:'{}'}],final_text:'{"ok":true}'}]){
  await assert.rejects(requestModel(request,{spawnImpl:fakeSpawn(envelope)}),/MODEL_GPT_BAD_RESPONSE/);assert(!fs.existsSync(fakeSpawn.lastRoot));checks++;
 }
 await assert.rejects(requestModel({...request,tools:[]},{spawnImpl:fakeSpawn({kind:'tool_calls',tool_calls:[{name:tool.name,input_json:'{}'}],final_text:''})}),/MODEL_GPT_BAD_RESPONSE/);checks++;
 const forbidden=complete+'\n'+JSON.stringify({type:'item.completed',item:{type:'command_execution',command:'must never execute'}});
 await assert.rejects(requestModel(request,{spawnImpl:fakeSpawn(undefined,{raw:forbidden})}),e=>e.diagnostic.reason==='native_tool_blocked');checks++;
 await assert.rejects(requestModel(request,{spawnImpl:fakeSpawn(undefined,{stderr:'ERROR codex_core::tools::router: error=code-mode host is disabled'})}),e=>e.diagnostic.reason==='native_tool_blocked');checks++;
 await assert.rejects(requestModel(request,{spawnImpl:fakeSpawn(undefined,{hang:true}),timeoutMs:5}),/MODEL_GPT_TIMEOUT/);assert.equal(fakeSpawn.lastSignal,'SIGTERM');assert(!fs.existsSync(fakeSpawn.lastRoot));checks++;
 for(const [error,code] of [['requires a newer version of Codex','CONFIG'],['You have hit your usage limit','QUOTA'],['rate limit 429','RATE_LIMIT'],['not logged in','AUTH'],['request timeout','TIMEOUT']]){assert.equal(bridge.classifyFailure(error).code,'MODEL_GPT_'+code);checks++;}
 const sensitive='test-secret-and-case-document';assert(!bridge.failureOutput(Error(sensitive)).includes(sensitive));assert.deepEqual(bridge.childEnvironment({HOME:'/home',OPENAI_API_KEY:sensitive,CODEX_API_KEY:sensitive,NODE_OPTIONS:sensitive,CODEX_THREAD_ID:'parent',PATH:'/bin'}),{HOME:'/home',PATH:'/bin'});checks++;
 assert.equal(result.runtime.cli_version,bridge.VERIFIED_CLI_VERSION);
 await assert.rejects(requestModel(request,{versionRunner:()=>"codex-cli 0.149.1",spawnImpl:()=>{throw Error('must not spawn');}}),/MODEL_GPT_CONFIG/);checks++;
 await assert.rejects(requestModel(request,{spawnImpl:fakeSpawn(undefined,{stderr:['x'.repeat(1048600),'ERROR codex_core::tools::','router: error=code-mode host is disabled']})}),e=>e.diagnostic.reason==='native_tool_blocked');checks++;
 const startupWarning='Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in /synthetic-home/.codex/config.toml.';
 const withWarning=JSON.stringify({type:'item.completed',item:{type:'error',message:startupWarning}})+'\n'+complete;
 const accepted=await requestModel(request,{spawnImpl:fakeSpawn({kind:'final',tool_calls:[],final_text:'{"ok":true}'},{raw:withWarning})});
 assert(accepted.runtime.warnings.includes('host_skill_discovery_disabled_unstable'));assert(accepted.runtime.warnings.includes('code_mode_host_disabled_fail_closed'));checks++;
 for(const message of [startupWarning.replace('skip_host_skill_discovery.','skip_host_skill_discovery, unexpected_feature.'),startupWarning+'\nUnexpected runtime failure']){
  await assert.rejects(requestModel(request,{spawnImpl:fakeSpawn(undefined,{raw:JSON.stringify({type:'item.completed',item:{type:'error',message}})+'\n'+complete})}));checks++;
 }
 console.log(`${checks} isolated GPT bridge checks passed`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
