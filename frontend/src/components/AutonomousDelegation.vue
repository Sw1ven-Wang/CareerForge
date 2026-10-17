<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import {useRouter} from 'vue-router'
import {ElMessage,ElMessageBox} from 'element-plus'
import {api,mutation} from '../api/client'
import {activeStates,type StudyTrack} from '../types/study'
import type {DelegationReport} from '../types/delegation'
const props=defineProps<{track:StudyTrack;tokenCap:number}>(),emit=defineEmits<{updated:[]}>()
const router=useRouter(),write=mutation()
const objective=ref(''),keys=ref<string[]>([]),candidates=ref<string[]>([]),budget=ref(6),parentCalls=ref(3),busy=ref(false)
const reports=ref<DelegationReport[]>([])
const roles=ref(['requirement_analysis','code_investigation','evidence_review','learning_design'])
const roleLabels:Record<string,string>={requirement_analysis:'需求分析',code_investigation:'代码调查',evidence_review:'证据复核',learning_design:'学习设计'}
const running=computed(()=>!!props.track.active_run&&activeStates.includes(props.track.active_run.status))
let serial=0
async function load(){
 const version=++serial,tid=props.track.track_id
 try{const result=await api<DelegationReport[]>('/study/tracks/'+tid+'/delegations');if(serial===version&&props.track.track_id===tid)reports.value=result}catch{reports.value=[]}
}
watch(()=>props.track.track_id,()=>{objective.value='';keys.value=props.track.requirements?.requirements.slice(0,8).map(r=>r.key)??[];candidates.value=[];reports.value=[]},{immediate:true})
watch(()=>[props.track.track_id,props.track.revision,props.track.active_run?.status],load,{immediate:true})
async function start(){
 if(!keys.value.length||!objective.value.trim())return
 try{
 await ElMessageBox.confirm(`父 Agent 自主选择只读角色和任务范围，最多两个子任务。总计最多 ${budget.value} 次模型调用（含父任务决策与汇总），父任务最多 ${parentCalls.value} 次，每次最多 ${props.tokenCap} 输出 token。仅使用所选技术和固定项目，不执行命令或修改已有计划。供应商按实际用量计费。`,'启动自主委派',{confirmButtonText:'启动本轮',cancelButtonText:'取消'})
 busy.value=true
 await write('/study/tracks/'+props.track.track_id+'/delegations','POST',{expected_revision:props.track.revision,objective:objective.value,requirement_keys:keys.value,candidate_ids:candidates.value,allowed_roles:roles.value,call_budget:budget.value,parent_call_limit:parentCalls.value,max_output_tokens:props.tokenCap})
 emit('updated')
 }catch(e){if(e!=='cancel'&&e!=='close')ElMessage.error(String(e))}finally{busy.value=false}
}
</script>
<template>
<details class="delegation surface"><summary>让 Agent 自主拆分调查任务</summary>
<p class="muted">描述问题，选择允许读取的技术和项目。Agent 自行决定是否委派、角色及额度；输出调查说明和证据，正式审核、计划与人工验收仍从各自入口进行。</p>
<el-input v-model="objective" type="textarea" :rows="3" maxlength="3000" placeholder="例如：调查工具注册与参数校验的实现，复核边界，再建议如何学习。"/>
<div class="scope"><el-select v-model="keys" multiple :multiple-limit="8" placeholder="允许分析的技术目标"><el-option v-for="r in track.requirements?.requirements" :key="r.key" :value="r.key" :label="r.technology+' · '+r.depth"/></el-select><el-select v-model="candidates" multiple :multiple-limit="2" placeholder="允许读取的项目（可选，需已留存固定版本）"><el-option v-for="c in track.candidates" :key="c.id" :value="c.id" :label="c.full_name"/></el-select></div>
<details class="settings"><summary>角色与预算</summary><el-checkbox-group v-model="roles"><el-checkbox v-for="(label,key) in roleLabels" :key="key" :value="key">{{label}}</el-checkbox></el-checkbox-group><label>总调用额度 <el-input-number v-model="budget" :min="3" :max="10"/></label><label>父任务调用上限 <el-input-number v-model="parentCalls" :min="2" :max="Math.min(4,budget)"/></label><small>总额度包含所有父子调用，预留一次最终汇总；未知请求不退款。15 分钟截止，取消会传播。</small></details>
<el-button type="primary" :loading="busy" :disabled="running||track.independent_attempt_active||!track.requirements||!keys.length||!roles.length||!objective.trim()" @click="start">启动自主委派</el-button><p v-if="track.independent_attempt_active" class="muted">独立练习中暂停本系统辅助，请先提交或结束该次尝试。</p>
<article v-for="report in reports" :key="report.id" class="report"><header><strong>{{report.objective}}</strong><el-tag v-if="report.synthetic" type="warning" size="small">离线模拟</el-tag><router-link :to="'/runs/'+report.run_id">任务与预算</router-link></header><p>{{report.summary}}</p><ul><li v-for="(claim,index) in report.claims" :key="index">{{claim.statement}} <small>依据 {{claim.evidence_ids.length}} 处</small></li></ul><details><summary>原文依据（位置已校验，语义支持未独立核验）</summary><blockquote v-for="e in report.evidence" :key="e.evidence_id"><el-button v-if="e.citation_id" link @click="router.push({path:'/studio',query:{citation:e.citation_id}})">{{e.path}}:{{e.start_line}}–{{e.end_line}}</el-button><small v-else>{{e.source_title}}</small><pre>{{e.quote}}</pre></blockquote></details><p v-if="report.unresolved.length" class="muted">待确认：{{report.unresolved.join('；')}}</p><p v-if="report.counterevidence.length" class="muted">反证与边界：{{report.counterevidence.join('；')}}</p><small>已占用 {{report.budget.consumed_calls}} / {{report.budget.call_limit}} 次 · 停止原因 {{report.stop_reason}} · 未运行代码或确认能力掌握</small></article>
</details>
</template>
<style scoped>
.delegation{padding:18px;margin:16px 0}summary{cursor:pointer;color:#426a51;font-size:13px}.scope{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}.settings{margin:12px 0}.settings label{display:inline-flex;align-items:center;gap:8px;margin:10px 16px 10px 0}.settings small{display:block}.muted,small{font-size:12px;color:#738177;line-height:1.8}.report{margin-top:18px;padding-top:16px;border-top:1px solid #e0e8e1;font-size:13px;line-height:1.8}.report header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}blockquote{margin:10px 0;padding:10px;background:#f2f6f2;border-left:3px solid #a8c3b0}pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:700px){.scope{grid-template-columns:1fr}}
</style>
