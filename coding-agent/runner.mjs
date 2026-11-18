/** Derived from CareerForge main@90e32f1 coding-agent/runner.mjs (MIT).
 * All inference and tools cross the Python gateway. No provider secret is present.
 */
import { createInterface } from "node:readline";
import { createAgentSession, createExtensionRuntime, ModelRuntime, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
globalThis.fetch = async () => { throw new Error("Use CareerForge gateway"); };
const write = value => {
  const frame = JSON.stringify(value) + "\n";
  if (Buffer.byteLength(frame) > 1000000) throw new Error("Frame too large");
  process.stdout.write(frame);
};
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Map();
let initialResolve, initialReject, initialized = false, session, seq = 0;
const initial = new Promise((resolve, reject) => { initialResolve = resolve; initialReject = reject; });
lines.on("line", line => {
  try {
    if (Buffer.byteLength(line) > 1000000) throw new Error("Frame too large");
    const value = JSON.parse(line);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid packet");
    if (!initialized) { initialized = true; initialResolve(value); return; }
    const waiter = pending.get(value.id);
    if (!waiter || !Number.isSafeInteger(value.id) || Object.keys(value).sort().join(",") !== "id,result") throw new Error("Unexpected response");
    pending.delete(value.id);
    waiter.resolve(value.result);
  } catch { initialReject(new Error("Invalid gateway packet")); process.exitCode = 1; lines.close(); }
});
lines.on("close", () => { initialReject(new Error("Gateway disconnected")); for (const w of pending.values()) w.reject(new Error("Gateway disconnected")); pending.clear(); session?.abort().catch(() => {}); });
function rpc(type, payload) { const id = ++seq; return new Promise((resolve, reject) => { pending.set(id, {resolve,reject}); write({type,id,...payload}); }); }
async function main() {
  const init = await initial;
  if (init.protocol !== 1 || !Number.isSafeInteger(init.call_limit) || init.call_limit < 1 || init.call_limit > 4
      || typeof init.prompt !== "string" || typeof init.system_prompt !== "string" || !Array.isArray(init.tools)) throw new Error("Invalid initialization");
  let proposed = false, calls = 0;
  const runtime = await ModelRuntime.create({authPath:process.cwd()+"/auth.json",modelsPath:null,modelsStorePath:process.cwd()+"/models.json",allowModelNetwork:false,refreshOnCreate:false});
  await runtime.setRuntimeApiKey("openai","careerforge-gateway-only");
  const resourceLoader = {
    getExtensions:()=>({extensions:[],errors:[],runtime:createExtensionRuntime()}),getSkills:()=>({skills:[],diagnostics:[]}),
    getPrompts:()=>({prompts:[],diagnostics:[]}),getThemes:()=>({themes:[],diagnostics:[]}),getAgentsFiles:()=>({agentsFiles:[]}),
    getSystemPrompt:()=>init.system_prompt,getSystemPromptSource:()=>undefined,getAppendSystemPrompt:()=>[],getAppendSystemPromptSources:()=>[],extendResources:()=>{},reload:async()=>{}
  };
  const customTools = init.tools.map(tool=>({...tool,label:tool.name,async execute(id,args){
    if(proposed) throw new Error("Proposal already submitted");
    const result = await rpc("tool",{name:tool.name,arguments:args,call_id:id});
    if(tool.name==="submit_reply"&&!result.is_error) proposed=true;
    return {content:[{type:"text",text:JSON.stringify(result)}],details:result,isError:!!result.is_error};
  }}));
  const model = {id:init.model,name:init.model,api:"openai-completions",provider:"openai",baseUrl:"http://127.0.0.1:9/disabled",reasoning:true,input:["text"],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:128000,maxTokens:init.max_output_tokens};
  ({session}=await createAgentSession({cwd:process.cwd(),agentDir:process.cwd(),modelRuntime:runtime,model,thinkingLevel:"low",tools:customTools.map(x=>x.name),customTools,resourceLoader,
    sessionManager:SessionManager.inMemory(process.cwd()),settingsManager:SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false,maxRetries:0},cacheWarming:"off"})}));
  session.agent.toolExecution="sequential";
  session.agent.finishTurn=()=>proposed||calls>=init.call_limit?{action:"end"}:undefined;
  session.agent.streamFunction=()=>{
    const stream=createAssistantMessageEventStream();
    const msg={role:"assistant",api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),content:[],stopReason:"stop",usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
    queueMicrotask(async()=>{
      try {
        if(calls>=init.call_limit) throw new Error("Budget exhausted");calls++;
        const result=await rpc("model",{});
        if(result.text) msg.content.push({type:"text",text:result.text});
        for(const call of result.tool_calls) msg.content.push({type:"toolCall",id:call.id,name:call.name,arguments:call.arguments});
        msg.stopReason=result.finish_reason==="tool_calls"?"toolUse":"stop";
        stream.push({type:"start",partial:msg});stream.push({type:"done",reason:msg.stopReason,message:msg});stream.end(msg);
      } catch(error) {msg.stopReason="error";msg.errorMessage=String(error.message);stream.push({type:"error",reason:"error",error:msg});stream.end(msg);}
    });return stream;
  };
  write({type:"ready",sdk:"pi-coding-agent-1.3.0",protocol:1,tools:session.agent.state.tools.map(x=>x.name)});
  try {await session.prompt(init.prompt,{expandPromptTemplates:false});const last=session.agent.state.messages.filter(x=>x.role==="assistant").at(-1);write({type:"done",proposed,calls,failed:last?.stopReason==="error"||last?.stopReason==="aborted"});}
  finally {session.dispose();lines.close();}
}
main().catch(error=>{write({type:"failed",code:"PI_RUNTIME_ERROR"});console.error(error.message);lines.close();process.exitCode=1;});
