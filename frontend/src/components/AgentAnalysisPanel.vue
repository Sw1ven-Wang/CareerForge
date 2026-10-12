<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api/client'
import { ElMessage } from 'element-plus'
type Expr={type:string;concept_id?:string;field?:string;comparator?:string;value?:unknown;children?:Expr[]}
type Fact={requirement_id:string;importance:string;requirement_kind:string;expression:Expr;evidence:{section_id:string;start:number;end:number;quote:string}}
type Job={job_snapshot_id:string;title:string;facts:Fact[];unresolved:string[];error?:string;usage?:{input_tokens:number;output_tokens:number}}
const props=defineProps<{value:{model:string;reasoning_effort:string;candidate_count:number;failed_jobs:number;jobs:Job[]}}>()
const evidence=ref<{text:string;before:string;after:string}|null>(null)
const labels:Record<string,string>={required:'必需',preferred:'优先',responsibility:'职责',unknown:'未明确',technical_skill:'技术技能',engineering_capability:'工程能力',knowledge:'知识',eligibility:'资格条件',other:'其他',gte:'至少',lte:'最多',eq:'等于',in:'属于'}
function describe(e:Expr):string {
  if(e.children) return '('+e.children.map(describe).join(e.type==='any_of'?' 或 ':' 且 ')+')'
  if(e.concept_id) return e.concept_id.replace(/^(technology|capability|knowledge)\./, '')
  return `${e.field} ${labels[e.comparator ?? ''] ?? e.comparator} ${JSON.stringify(e.value)}`
}
async function inspect(job:Job,fact:Fact) {
  try {
    const row=await api<{snapshot:{document:{canonical_text:string}}}>(`/job-data/artifacts/${job.job_snapshot_id}`)
    const chars=Array.from(row.snapshot.document.canonical_text)
    evidence.value={before:chars.slice(0,fact.evidence.start).join(''),text:chars.slice(fact.evidence.start,fact.evidence.end).join(''),after:chars.slice(fact.evidence.end).join('')}
  } catch(e) {ElMessage.error(String(e))}
}
</script>
<template><section class="table-card" style="padding:24px;margin:20px 0"><h2>Agent 岗位要求分析报告</h2><p>{{props.value.model}} · {{props.value.reasoning_effort}} · {{props.value.candidate_count}} 条有原文依据的候选 · {{props.value.failed_jobs}} 份分析失败</p><el-alert title="以下为模型提取候选，已校验引用与正文一致。请在资料库核对并确认，再用离线整理刷新正式统计与学习计划。每份最多提取 4 条重点候选，不代表覆盖全部要求。未明确归一的内容保留为待确认项。" type="info" :closable="false"/><article v-for="job in value.jobs" :key="job.job_snapshot_id"><h3>{{job.title}}</h3><p>固定岗位版本：{{job.job_snapshot_id}}</p><el-alert v-if="job.error" :title="`本份未生成有效分析：${job.error}；未自动重试。`" type="warning" :closable="false"/><p v-if="job.usage">本次 token：输入 {{job.usage.input_tokens}} / 输出 {{job.usage.output_tokens}}（金额未定价）</p><el-table :data="job.facts"><el-table-column label="要求分析" min-width="220"><template #default="{row}">{{describe(row.expression)}}</template></el-table-column><el-table-column label="分类" width="110"><template #default="{row}">{{labels[row.requirement_kind]}}</template></el-table-column><el-table-column label="强度" width="85"><template #default="{row}">{{labels[row.importance]}}</template></el-table-column><el-table-column prop="evidence.quote" label="原文依据" min-width="260"/><el-table-column label="核查" width="100"><template #default="{row}"><el-button link @click="inspect(job,row)">定位原文</el-button></template></el-table-column></el-table><p v-if="!job.facts.length&&!job.error">未提取到通过引用校验的要求。</p><h4 v-if="job.unresolved.length">待确认 / 未归一内容</h4><p v-for="(item,i) in job.unresolved" :key="i">{{item}}</p></article></section><el-drawer :model-value="!!evidence" @close="evidence=null" title="冻结岗位正文与原文依据" size="min(760px,100vw)"><pre v-if="evidence" style="white-space:pre-wrap">{{evidence.before}}<mark>{{evidence.text}}</mark>{{evidence.after}}</pre></el-drawer></template>
