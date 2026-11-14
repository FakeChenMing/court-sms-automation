// Minimal bounded Streamable HTTP MCP client; config is supplied privately on stdin.
const fs=require('fs');
async function main(){
 const q=JSON.parse(fs.readFileSync(0,'utf8'));const dest=new URL(q.url);
 if(dest.protocol!=='https:'||!['apim-gateway.pkulaw.com','open.chineselaw.com'].includes(dest.hostname))throw new Error('HOST_DENIED');
 let session=null;let id=0;
 async function send(method,params,notification=false){
  const request={jsonrpc:'2.0',method,params};if(!notification)request.id=++id;
  const headers={...q.headers,'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-03-26'};if(session)headers['Mcp-Session-Id']=session;
  const r=await fetch(dest,{method:'POST',headers,body:JSON.stringify(request),redirect:'error',signal:AbortSignal.timeout(75000)});
  if(!r.ok)throw new Error('HTTP_'+r.status);session=r.headers.get('mcp-session-id')||session;
  if(notification||r.status===202)return null;
  if((r.headers.get('content-type')||'').includes('application/json')){const x=await r.json();if(x.error)throw new Error('RPC_'+x.error.code);return x.result;}
  const reader=r.body.getReader();const decoder=new TextDecoder();let buffer='';
  try{while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});buffer=buffer.replace(/\r\n/g,'\n');if(buffer.length>16000000)throw new Error('RESULT_TOO_LARGE');let split;
   while((split=buffer.indexOf('\n\n'))>=0){const event=buffer.slice(0,split);buffer=buffer.slice(split+2);const data=event.split('\n').filter(s=>s.startsWith('data:')).map(s=>s.slice(5).trim()).join('\n');if(!data)continue;let x;try{x=JSON.parse(data);}catch{continue;}if(x.id===request.id){if(x.error)throw new Error('RPC_'+x.error.code);return x.result;}}
  }}finally{await reader.cancel();}
  throw new Error('NO_RESULT');
 }
 await send('initialize',{protocolVersion:'2025-03-26',capabilities:{},clientInfo:{name:'court-document-workflow',version:'1.0'}});
 await send('notifications/initialized',{},true);
 const result=await send(q.method,q.arguments||{});process.stdout.write(JSON.stringify(result));
}
main().catch(e=>{process.stdout.write(JSON.stringify({transport_error:/^(HOST_DENIED|HTTP_\d+|RPC_-?\d+|RESULT_TOO_LARGE|NO_RESULT)$/.test(e.message)?e.message:e.name}));process.exitCode=1;});
