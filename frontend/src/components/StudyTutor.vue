<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, mutation } from '../api/client'
import { followRun } from '../api/runEvents'
import ContextTrace from './ContextTrace.vue'
import { type StudyTrack, type StudyPlan, type StudyModule, type Block, activeStates } from '../types/study'
const props=defineProps<{track:StudyTrack;plan:StudyPlan;module:StudyModule}>()
const emit=defineEmits<{changed:[];open:[id:string];point:[value:{citation_id:string;quote_start_line:number;quote_end_line:number}]}>()
const write=mutation(),question=ref(''),busy=ref(false),follow=ref(true),calls=ref(3),cap=ref(4096),events=ref<string[]>([])
const turns=computed(()=>props.track.turns.filter(t=>t.plan_id===props.plan.id&&t.module_key===props.module.key))
const active=computed(()=>!!props.track.active_run&&activeStates.includes(props.track.active_run.status))
let controller:AbortController|undefined,followed='',observedRun=''
watch(()=>props.track.active_run?.run_id, id=>{
 if(!id||id===observedRun||!active.value)return
 observedRun=id;controller?.abort();controller=new AbortController();events.value=[]
 void followRun(id,event=>{
  const labels:Record<string,string>={'run.started':'Agent 开始读取上下文','stage.completed':'阶段结果已保存','study.context':'上下文与证据指针已保存','study.tool':'已执行只读代码工具','run.finished':'本轮已结束','run.cancelled':'已停止','stage.failed':'结果未通过校验'}
  if(labels[event.type])events.value.push(labels[event.type]+(event.type==='study.tool'?' · '+String(event.payload?.name??''):''))
  emit('changed')
 },()=>{},controller.signal)
},{immediate:true})
watch(()=>turns.value.at(-1)?.id, id=>{
 const turn=turns.value.at(-1)
 if(id&&id!==followed){followed=id;if(follow.value&&turn?.focus_citation_id){const point=turn.points.find(p=>p.citation_id===turn.focus_citation_id);if(point)emit('point',point)}}
},{immediate:true})
onBeforeUnmount(()=>controller?.abort())
async function send(){
 if(!question.value.trim())return
 if(props.track.independent_attempt_active){ElMessage.warning('独立尝试进行中，请先提交或结束尝试');return}
 try{await ElMessageBox.confirm(`本轮最多 ${calls.value} 次模型调用，每次最多 ${cap.value} 输出 token，只读当前模块的审核项目。按供应商用量计费。`,'发送给指导 Agent',{confirmButtonText:'发送本轮问题',cancelButtonText:'取消'})
 busy.value=true
 await write('/study/tracks/'+props.track.track_id+'/runs','POST',{expected_revision:props.track.revision,role:'tutor',plan_id:props.plan.id,module_key:props.module.key,question:question.value,max_output_tokens:cap.value,model_call_limit:calls.value})
 question.value='';emit('changed')
 }catch(e){if(e!=='cancel'&&e!=='close')ElMessage.error(String(e))}finally{busy.value=false}
}
async function stop(){const run=props.track.active_run;if(!run)return;try{await api('/runs/'+run.run_id+'/cancel','POST',{expected_version:run.version});emit('changed')}catch(e){ElMessage.error(String(e))}}
function pos(block:Block,key:string){const i=block.nodes.findIndex(n=>n.key===key);return {x: i%2*350+35,y:Math.floor(i/2)*100+20}}
</script>
<template><div class="study-tutor"><header><h2>指导学习 Agent</h2><el-switch v-model="follow" active-text="跟随代码定位"/></header><p class="muted">当前模块：{{module.title}}<br>可随时提问；只读取此模块对应的审核项目。</p>
<el-alert v-if="track.independent_attempt_active" title="独立尝试进行中：导师问答暂时停用。历史解释仍可查看；提交或结束尝试后恢复。" type="warning" :closable="false"/><div class="turns"><article v-for="t in turns" :key="t.id" class="turn"><p class="question">{{t.question}}</p><el-tag v-if="t.synthetic" type="warning" size="small">离线模拟</el-tag><el-tag v-if="t.status==='insufficient_evidence'" type="info" size="small">依据不足</el-tag>
<section v-for="(b,i) in t.blocks" :key="i" class="block"><h4 v-if="b.title">{{b.title}}</h4><p v-if="b.kind==='text'" class="text">{{b.text}}</p>
<svg v-else-if="b.kind==='diagram'" :viewBox="'0 0 720 '+Math.ceil(b.nodes.length/2)*100" role="img" :aria-label="b.title||'Agent 学习图示'"><g v-for="(e,j) in b.edges" :key="'e'+j"><line :x1="pos(b,e.source).x+140" :y1="pos(b,e.source).y+32" :x2="pos(b,e.target).x+140" :y2="pos(b,e.target).y+32" stroke="#9aac9b" stroke-width="2"/><text :x="(pos(b,e.source).x+pos(b,e.target).x)/2+140" :y="(pos(b,e.source).y+pos(b,e.target).y)/2+25" text-anchor="middle" font-size="12" fill="#4e6a58">{{e.label}}</text></g><g v-for="n in b.nodes" :key="n.key"><rect :x="pos(b,n.key).x" :y="pos(b,n.key).y" width="280" height="64" rx="10" fill="#e7f1e8" stroke="#8db496"/><text :x="pos(b,n.key).x+140" :y="pos(b,n.key).y+37" text-anchor="middle" font-size="15" fill="#2c5840">{{n.label.length>19?n.label.slice(0,19)+'…':n.label}}</text><title>{{n.label}}</title></g></svg>
<div v-else-if="b.kind==='table'" class="table-scroll"><table><thead><tr><th v-for="(c,j) in b.columns" :key="j">{{c}}</th></tr></thead><tbody><tr v-for="(r,j) in b.rows" :key="j"><td v-for="(cell,k) in r" :key="k">{{cell}}</td></tr></tbody></table></div></section>
<article v-for="p in t.points" :key="p.citation_id+p.title" class="code-point"><strong>{{p.title}}</strong><p>{{p.explanation}}</p><small>引用定位已校验 · 语义支持未独立核验 · 无运行观察</small><pre>{{p.quote}}</pre><el-button link @click="emit('point',p)">{{p.path}}:{{p.quote_start_line}}–{{p.quote_end_line}} · 查看引用位置</el-button></article>
<ContextTrace v-if="t.context_run_id" :track-id="track.track_id" :run-id="t.context_run_id" @open="id=>emit('open',id)"/><details v-if="t.limitations.length"><summary>范围与未知</summary><p v-for="l in t.limitations" :key="l" class="muted">{{l}}</p></details><div v-if="t.questions.length"><small>接下来可以问</small><el-button v-for="q in t.questions" :key="q" link @click="question=q">{{q}}</el-button></div></article>
<el-empty v-if="!turns.length" description="选择模块后，问一个问题开始学习。"/></div>
<ContextTrace v-if="track.active_run?.role==='tutor'&&active" :key="track.active_run.run_id" :track-id="track.track_id" :run-id="track.active_run.run_id" @open="id=>emit('open',id)"/><div v-if="active" class="live"><span class="pulse"></span> Agent 正在工作，工具进度实时更新；完整回答完成后显示。<el-button link @click="stop">停止本轮</el-button></div><ol v-if="events.length" class="muted"><li v-for="(e,i) in events.slice(-5)" :key="i">{{e}}</li></ol>
<el-input v-model="question" type="textarea" :rows="4" placeholder="这个模块为什么这样设计？请定位核心代码，并画出数据流。"/><div class="send"><el-button type="primary" :disabled="track.independent_attempt_active||active||!question.trim()" :loading="busy" @click="send">提问 / 继续学习</el-button></div>
<details><summary>模型预算</summary><p>gpt-6.1-sol · low</p><label>最多调用 <el-input-number v-model="calls" :min="1" :max="3" size="small"/></label><label>每次输出 <el-input-number v-model="cap" :min="512" :max="8192" :step="512" size="small"/></label></details>
<p class="muted">只读导学。切换自己的 IDE 后可编辑代码、请求补丁；命令执行仍需确认。</p>
</div></template>
<style scoped>
header{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between}h2{font-size:17px;margin:0}.muted,small{font-size:12px;color:#75847b;line-height:1.8}.turns{max-height:650px;overflow:auto}.turn{font-size:13px;border-bottom:1px solid #e1e9e2;padding-bottom:16px;margin-bottom:18px}.question{padding:12px;background:#ecf3ed;border-radius:8px;white-space:pre-wrap}.text,.code-point p{line-height:1.85;white-space:pre-wrap}.block h4{margin-bottom:8px}.block svg{width:100%;min-height:130px;background:#fbfcfa;border-radius:8px}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #dce5dc;padding:7px;text-align:left}th{background:#edf3ed}.code-point{margin:14px 0;padding:12px;background:#f5f8f5;border-radius:7px}.code-point pre{white-space:pre-wrap;font:12px/1.7 Consolas,monospace;overflow:auto;max-height:180px}.code-point .el-button,.turn .el-button{white-space:normal;line-height:1.7;height:auto;text-align:left}.turn small{display:block}.turn>div>.el-button{display:block;margin-left:0}.live{font-size:12px;line-height:1.8;color:#426c50;padding:10px}.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:#5c9667;margin-right:6px}.send{margin:10px 0}summary{font-size:12px;color:#657e6b;cursor:pointer}details{margin:12px 0}label{display:flex;font-size:12px;gap:10px;margin:8px 0;align-items:center}
</style>
