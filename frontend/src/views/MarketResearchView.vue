<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, apiPage, mutation } from '../api/client'
import RunPanel from '../components/RunPanel.vue'
import type { components } from '../types/generated_api'
type Extraction = Omit<components['schemas']['RequirementCorrection'], 'expected_version' | 'reason'>
type Requirement = Extraction & {posting_version_id:string;requirement_id:string; collection_id:string; collection_version:number;
  citation:{citation_id:string;source_id:string;source_snapshot_id:string;char_start:number;char_end:number}}
type Goal = {goal_version_id:string;role_direction:string;job_type:string;regions:string[]}
type Source = {source_id:string;title:string|null;source_type:string;read_status:string}
type Snapshot = {source_snapshot_id:string;version_no:number;metadata:{source_type:string}}
type Report = {market_report_id:string;generated_by_run_id:string;source_snapshot_ids:string[];
  input_summaries:{source_snapshot_id:string;source_type:string;title:string|null;read_status:string}[];eligible_posting_version_ids:string[];counts:Record<string,number>;warnings:string[];requirements:Requirement[];
  filters:{job_type:string;regions:string[]};
  skill_statistics:{skill_concept_id:string;numerator:number;denominator:number;ratio:number|null;supporting_posting_version_ids:string[];denominator_posting_version_ids:string[]}[];
  unresolved_sources:{source_snapshot_id:string;reason:string}[];
  posting_summaries:{posting_version_id:string;source_snapshot_id:string;source_id:string;title:string|null;job_type:string|null;regions:string[]}[];
  scope_exclusions:{posting_version_id:string;reason:string}[];
  dedup_groups:string[][]; regional_posting_groups:Record<string,string[]>}
type Candidate = {candidate_id:string;posting_version_ids:string[];reason:string;version:number;
 latest_decision:null|{decision_id:string;decision:string;reason:string}}
const goals=ref<Goal[]>([]), goalId=ref(''), choices=ref<{id:string;label:string;kind:string}[]>([])
const chosen=ref<string[]>([]), report=ref<Report|null>(null), runId=ref(''), busy=ref(false)
const reports=ref<{market_report_id:string;created_at:string}[]>([]), candidates=ref<Candidate[]>([])
const decisionIds=ref<string[]>([]), nextReportCursor=ref<string|null>(null)
const quote=ref<{content:string;before:string;text:string;after:string}|null>(null)
const correction=ref<Extraction|null>(null), editingId=ref(''), collectionVersion=ref(1), correctionReason=ref('')
const decisionForm=reactive({candidate_id:'',version:1,decision:'unknown',reason:''})
const write=mutation(), correctionWrite=mutation(), decisionWrite=mutation()
const countNames:Record<string,string>={raw:'输入材料',readable:'正文可读',job_postings:'岗位正文',
 extraction_completed:'完成规则提取',deduplicated:'确定去重后岗位',interview_experience:'面经（不计分母）',other_material:'其他材料（不计分母）',unresolved_duplicates:'未决重复对'}
const typeNames:Record<string,string>={must:'必须项',preferred:'优先项',responsibility:'职责',inferred:'推断项'}
const scopedJobCount=computed(()=>report.value?.eligible_posting_version_ids.length ?? 0)
async function load() {
  try {
    goals.value=await api<Goal[]>('/career-goals?limit=100')
    if (!goalId.value) goalId.value=goals.value[0]?.goal_version_id ?? ''
    const rows:Source[]=[]; let cursor:string|null=null
    do {
      const page: {data:Source[];next_cursor?:string|null}=await apiPage<Source>('/sources?limit=100'+(cursor?`&cursor=${encodeURIComponent(cursor)}`:''))
      rows.push(...page.data);cursor=page.next_cursor ?? null
    } while(cursor)
    const sourceDetails=await Promise.all(rows.filter(r=>r.read_status==='available').map(async row=>{
      const detail=await api<{snapshots:Snapshot[]}>(`/sources/${row.source_id}`)
      return detail.snapshots.map(s=>({id:s.source_snapshot_id,kind:s.metadata.source_type,
        label:`${row.title ?? '未命名材料'} · v${s.version_no} · ${s.metadata.source_type==='job_posting'?'JD':s.metadata.source_type==='interview_experience'?'面经':'非岗位材料'}`}))
    }))
    choices.value=sourceDetails.flat(); await loadReports()
  } catch(e) {ElMessage.error(String(e))}
}
async function loadReports(more=false) {
  const page=await apiPage<{market_report_id:string;created_at:string}>('/market-reports?limit=20'+
    (goalId.value?`&goal_version_id=${goalId.value}`:'')+(more&&nextReportCursor.value?`&cursor=${nextReportCursor.value}`:''))
  reports.value=more?[...reports.value,...page.data]:page.data;nextReportCursor.value=page.next_cursor??null
}
async function viewReport(id:string) {
  try {
    report.value=await api<Report>(`/market-reports/${id}`)
    runId.value=report.value.generated_by_run_id
    const all:Candidate[]=[]; let cursor:string|null=null
    do {
      const page:{data:Candidate[];next_cursor?:string|null}=await apiPage<Candidate>(`/duplicate-candidates?goal_version_id=${goalId.value}&limit=100`+(cursor?`&cursor=${cursor}`:''))
      all.push(...page.data);cursor=page.next_cursor??null
    } while(cursor)
    const ids=new Set(report.value.posting_summaries.map(p=>p.posting_version_id))
    candidates.value=all.filter(c=>c.posting_version_ids.every(id=>ids.has(id)))
  } catch(e) {ElMessage.error(String(e))}
}
async function start() {
  busy.value=true
  try {
    const run=await write<{run_id:string}>('/market-research-runs','POST',{
      goal_version_id:goalId.value,source_snapshot_ids:chosen.value,retrieval_mode:'import_only',
      dedup_decision_ids:decisionIds.value,budget_profile:'development_small',source_queries:[],
    });runId.value=run.run_id;report.value=null
  } catch(e) {ElMessage.error(String(e))} finally {busy.value=false}
}
async function progress() {
  try {
    await loadReports()
    const current=await api<{output_refs:{market_report_id?:string}[]}>(`/runs/${runId.value}`)
    const output=current.output_refs.find(x=>x.market_report_id)
    if(output?.market_report_id && report.value?.market_report_id!==output.market_report_id) await viewReport(output.market_report_id)
  } catch(e) {ElMessage.error(String(e))}
}
async function readQuote(row:Requirement) {
  try {
    const value=await api<{content:string}>(`/sources/${row.citation.source_id}/snapshots/${row.citation.source_snapshot_id}`)
    const chars=Array.from(value.content)
    quote.value={content:value.content,before:chars.slice(0,row.char_start).join(''),text:chars.slice(row.char_start,row.char_end).join(''),after:chars.slice(row.char_end).join('')}
  } catch(e) {ElMessage.error(String(e))}
}
async function edit(row:Requirement) {
  try {
    const current=await api<Requirement & {current_collection_version:number;superseded_by_requirement_id:string|null}>(`/requirements/${row.requirement_id}`)
    if(current.superseded_by_requirement_id) throw new Error('此条目已有更正，请重新分析后编辑最新条目。')
    const {requirement_type,origin,normalized_skill,skill_kind,text,char_start,char_end,logical_group}=current
    correction.value={requirement_type,origin,normalized_skill,skill_kind,text,char_start,char_end,logical_group}
    editingId.value=current.requirement_id;collectionVersion.value=current.current_collection_version;correctionReason.value=''
  } catch(e) {ElMessage.error(String(e))}
}
async function saveCorrection() {
  try {
    await correctionWrite(`/requirements/${editingId.value}/corrections`,'POST',{
      ...correction.value,origin:correction.value?.requirement_type==='inferred'?'inferred':'explicit',expected_version:collectionVersion.value,reason:correctionReason.value})
    correction.value=null;ElMessage.success('已保存新条目；本报告保留原值，重新分析可使用更正。')
  } catch(e) {ElMessage.error(String(e))}
}
async function saveDecision() {
  try {
    const value=await decisionWrite<{decision_id:string}>(`/duplicate-candidates/${decisionForm.candidate_id}/decisions`,'POST',{
      expected_version:decisionForm.version,decision:decisionForm.decision,reason:decisionForm.reason})
    decisionIds.value=decisionIds.value.filter(id=>id!==candidates.value.find(c=>c.candidate_id===decisionForm.candidate_id)?.latest_decision?.decision_id)
    decisionIds.value.push(value.decision_id);decisionForm.candidate_id=''
    ElMessage.success('已保存决定；勾选并重新分析后生效，旧报告不变。')
    if(report.value) await viewReport(report.value.market_report_id)
  } catch(e) {ElMessage.error(String(e))}
}
onMounted(load)
</script>
<template>
  <section class="page-heading"><div><span class="eyebrow">岗位材料 → 可核查的要求</span><h1>岗位要求分析</h1><p>每项要求都有原文，每个比例都有明确的岗位分母。</p></div></section>
  <el-alert title="离线验证模式：规则产生候选，FakeModel 验证输出契约；未调用真实模型，不能据此评价模型提取质量。" type="warning" :closable="false" style="margin-bottom:20px"/>
  <section class="table-card" style="padding:24px;margin-bottom:24px">
    <el-form label-position="top">
      <el-form-item label="职业目标的固定版本"><el-select v-model="goalId" @change="report=null;runId='';decisionIds=[];candidates=[];loadReports()"><el-option v-for="g in goals" :key="g.goal_version_id" :value="g.goal_version_id" :label="`${g.role_direction} · ${g.job_type} · ${g.regions.join('、') || '地区未限定'}`"/></el-select></el-form-item>
      <el-form-item label="选择具体材料快照（最多 20 份，至少一份 JD）"><el-select v-model="chosen" multiple filterable :multiple-limit="20"><el-option v-for="s in choices" :key="s.id" :label="s.label" :value="s.id"/></el-select></el-form-item>
      <p>面经和个人材料只显示材料覆盖，不进入岗位分母。未读链接不能选择。</p>
      <el-button type="primary" :loading="busy" :disabled="!goalId || !chosen.length" @click="start">开始离线分析</el-button><el-button @click="load">刷新材料</el-button>
    </el-form>
  </section>
  <section v-if="reports.length" style="margin-bottom:24px"><h2>已保存报告</h2><el-button v-for="r in reports" :key="r.market_report_id" @click="viewReport(r.market_report_id)">{{new Date(r.created_at).toLocaleString()}}</el-button><el-button v-if="nextReportCursor" @click="loadReports(true)">更早报告</el-button></section>
  <RunPanel v-if="runId" :key="runId" :run-id="runId" @changed="progress" />
  <section v-if="report" class="table-card" style="padding:24px;margin-top:24px">
    <h2>本次样本与统计</h2><p>岗位类别：{{report.filters.job_type}} · 地区：{{report.filters.regions.join('、') || '未限定'}} · 同范围有效分母：{{scopedJobCount}}</p>
    <div class="goal-grid"><div v-for="(count,key) in report.counts" :key="key"><strong>{{count}}</strong> {{countNames[key] ?? key}}</div></div>
    <el-alert v-for="warning in report.warnings" :key="warning" :title="warning" type="info" :closable="false" style="margin-top:8px"/>
    <h3>输入材料与岗位分母</h3>
    <el-table :data="report.input_summaries"><el-table-column prop="title" label="材料"/><el-table-column prop="source_type" label="材料类型"/><el-table-column prop="read_status" label="正文状态"/></el-table>
    <el-table :data="report.posting_summaries"><el-table-column prop="title" label="岗位"/><el-table-column label="提取类别"><template #default="{row}">{{row.job_type ?? '未知'}}</template></el-table-column><el-table-column label="地区"><template #default="{row}">{{row.regions.join('、') || '未知'}}</template></el-table-column><el-table-column label="进入技能分母"><template #default="{row}">{{report.eligible_posting_version_ids.includes(row.posting_version_id)?'是（去重代表）':'否（范围未知、不符、失败或重复）'}}</template></el-table-column></el-table>
    <h3>技能出现率（按岗位计数）</h3>
    <el-table :data="report.skill_statistics"><el-table-column prop="skill_concept_id" label="技能 / 能力"/><el-table-column label="可复算数量"><template #default="{row}">{{row.numerator}} / {{row.denominator}}</template></el-table-column><el-table-column label="样本出现率"><template #default="{row}"><el-progress v-if="row.ratio!==null" :percentage="Math.round(row.ratio*100)"/><span v-else>分母不足</span></template></el-table-column><el-table-column label="支持岗位"><template #default="{row}">{{row.supporting_posting_version_ids.map((id:string)=>report?.posting_summaries.find(p=>p.posting_version_id===id)?.title ?? id.slice(0,8)).join('、') || '无'}}</template></el-table-column></el-table>
    <h3>岗位要求与原文依据</h3>
    <el-table :data="report.requirements"><el-table-column label="来源岗位" min-width="160"><template #default="{row}">{{report.posting_summaries.find(p=>p.posting_version_id===row.posting_version_id)?.title ?? row.posting_version_id}}<small v-if="!report.eligible_posting_version_ids.includes(row.posting_version_id)">（不计本次分母）</small></template></el-table-column><el-table-column prop="normalized_skill" label="概念"/><el-table-column label="类别"><template #default="{row}">{{typeNames[row.requirement_type]}}<small v-if="row.logical_group"> · 可选条件组</small></template></el-table-column><el-table-column prop="text" label="原文" min-width="240"/><el-table-column label="核查" width="150"><template #default="{row}"><el-button link @click="readQuote(row)">查看引用</el-button><el-button link @click="edit(row)">更正</el-button></template></el-table-column></el-table>
    <h3>范围排除与提取失败</h3><p v-if="!report.scope_exclusions.length&&!report.unresolved_sources.length">本次没有范围排除或提取失败；仍须人工检查规则遗漏。</p><p v-for="x in report.scope_exclusions" :key="x.posting_version_id">{{x.posting_version_id.slice(0,8)}}：岗位类别或地区不符合，或范围未知。</p><p v-for="x in report.unresolved_sources" :key="x.source_snapshot_id">{{x.source_snapshot_id.slice(0,8)}}：{{x.reason}}</p>
    <h3>疑似重复（确认前保留独立岗位）</h3>
    <div v-for="c in candidates" :key="c.candidate_id" style="margin:12px 0"><span>{{c.posting_version_ids.map(id=>report?.posting_summaries.find(p=>p.posting_version_id===id)?.title ?? id.slice(0,8)).join(' ↔ ')}} · {{c.latest_decision?.decision ?? '待确认'}}</span><el-button link @click="Object.assign(decisionForm,{candidate_id:c.candidate_id,version:c.version,decision:'unknown',reason:''})">判定</el-button><el-checkbox v-if="c.latest_decision" v-model="decisionIds" :value="c.latest_decision.decision_id">下次报告采用此决定</el-checkbox></div>
  </section>
  <el-drawer :model-value="!!quote" @close="quote=null" title="规范原文与引用位置" size="min(760px,100vw)"><pre v-if="quote" class="source-body">{{quote.before}}<mark>{{quote.text}}</mark>{{quote.after}}</pre></el-drawer>
  <el-dialog :model-value="!!correction" @close="correction=null" title="创建不可变更正条目" width="min(620px,95vw)"><el-form v-if="correction" label-position="top"><el-form-item label="要求类别"><el-select v-model="correction.requirement_type"><el-option v-for="(label,value) in typeNames" :key="value" :label="label" :value="value"/></el-select></el-form-item><el-form-item label="技能概念（必须有原文支持）"><el-input v-model="correction.normalized_skill"/></el-form-item><el-form-item label="支持原文（须与 Unicode 字符区间一致）"><el-input v-model="correction.text" type="textarea"/></el-form-item><el-form-item label="字符起点 / 终点（从 0 开始，左闭右开）"><el-input-number v-model="correction.char_start" :min="0"/><el-input-number v-model="correction.char_end" :min="1"/></el-form-item><el-form-item label="更正原因"><el-input v-model="correctionReason"/></el-form-item></el-form><template #footer><el-button type="primary" @click="saveCorrection">保存新条目</el-button></template></el-dialog>
  <el-dialog :model-value="!!decisionForm.candidate_id" @close="decisionForm.candidate_id=''" title="确认岗位重复关系" width="min(520px,95vw)"><el-select v-model="decisionForm.decision"><el-option label="同一岗位发布" value="duplicate"/><el-option label="不同岗位" value="distinct"/><el-option label="暂不确定" value="unknown"/></el-select><el-input v-model="decisionForm.reason" placeholder="判断依据" style="margin-top:16px"/><template #footer><el-button @click="saveDecision">保存决定</el-button></template></el-dialog>
</template>
