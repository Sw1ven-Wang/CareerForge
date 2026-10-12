<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, apiPage } from '../api/client'

type Source = {mock_id:string;title:string;source_id:string;source_snapshot_id:string}
type Criterion = {dimension:string;scenario:string;pass_rule:string;fail_rule:string;unknown_rule:string}
type Week = {id:string;title:string;effort_hours:{min:number;max:number};prerequisites:string[];activities:string[];deliverables:string[];criteria:Criterion[]}
type Research = {schema_version:string;is_synthetic:boolean;title:string;created_at:string;report_text:string;dataset_id:string;
  provenance:{confirmed_user_preferences:string[];assumptions:string[]};
  skill_frequency:{skill_id:string;skill:string;must_count:number;sample_count:number;must_ratio:number;preferred_count:number}[];
  personal_evidence:{id:string;project:string;basis:string;observed_claims:string[];does_not_prove:string[]}[];
  alignment:{skill_id:string;skill:string;related_evidence_ids:string[];personal_status:string;priority:string;reason:string;next_evidence:string}[];
  weeks:Week[];sources:Source[];goal_version_id:string;market_report_id:string;plan_id:string;plan_version_id:string;
  evaluation:{status:string;synthetic_question_target:number;development_count:number;frozen_validation_count:number;metrics:string[]};
  practice_mock:{title:string;conditions:string[];note:string}}
type Evidence = {evidence_id:string;title:string;kind:string}
const route=useRoute(), router=useRouter()
const report=ref<Research|null>(null), reports=ref<Evidence[]>([]), chosen=ref(''), busy=ref(false), error=ref(''), tab=ref('overview')
const planAvailable=ref(false), sourceText=ref(''), sourceTitle=ref(''), sourceDialog=ref(false), sourceBusy=ref(false), sourceError=ref('')
async function open(id:string) {
  busy.value=true;error.value='';report.value=null;planAvailable.value=false;chosen.value=id
  try {
    const item=await api<{versions:{kind:string;description:string}[]}>('/evidence/'+encodeURIComponent(id))
    const version=item.versions[0]
    if(!version || version.kind!=='mock_research_summary') throw new Error('这份材料不是模拟调研报告。')
    const parsed=JSON.parse(version.description) as Research
    if(parsed.schema_version!=='1.0' || parsed.is_synthetic!==true || !Array.isArray(parsed.weeks) || !Array.isArray(parsed.sources)) throw new Error('模拟报告格式不受支持。')
    report.value=parsed
    // Authorize the linked immutable plan before offering the plan editor.
    try {await api(`/plans/${parsed.plan_id}/versions/${parsed.plan_version_id}`);planAvailable.value=true} catch {planAvailable.value=false}
    await router.replace({path:'/mock-research',query:{report:id}})
  } catch(e) {error.value=e instanceof Error?e.message:String(e)} finally {busy.value=false}
}
async function load() {
  busy.value=true;error.value=''
  try {
    const all:Evidence[]=[];let cursor:string|null=null
    do {const page:{data:Evidence[];next_cursor?:string|null}=await apiPage<Evidence>('/evidence?limit=100'+(cursor?'&cursor='+encodeURIComponent(cursor):''));all.push(...page.data);cursor=page.next_cursor??null} while(cursor)
    reports.value=all.filter(e=>e.kind==='mock_research_summary')
    const requested=typeof route.query.report==='string'?route.query.report:''
    const id=requested || reports.value[0]?.evidence_id
    if(id) await open(id)
  } catch(e) {error.value=e instanceof Error?e.message:String(e)} finally {busy.value=false}
}
async function readSource(source:Source) {
  sourceTitle.value=source.title;sourceText.value='';sourceError.value='';sourceDialog.value=true;sourceBusy.value=true
  try {const saved=await api<{content:string}>(`/sources/${source.source_id}/snapshots/${source.source_snapshot_id}`);sourceText.value=saved.content}
  catch(e){sourceError.value=e instanceof Error?e.message:String(e)}finally{sourceBusy.value=false}
}
onMounted(load)
</script>
<template>
  <section class="page-heading"><div><span class="eyebrow">Agent 开发 · 个人职业准备</span><h1>模拟调研与学习计划</h1><p>查看岗位样本、材料分析和六周安排，逐项补充你本人的真实证据。</p></div><el-button :loading="busy" @click="load">刷新报告</el-button></section>
  <el-alert title="全部岗位为人工构造，不代表真实招聘市场；个人能力尚未验证，学习计划尚未采纳。" type="warning" :closable="false"/>
  <p v-if="reports.length"><el-select v-model="chosen" style="width:min(600px,100%)" aria-label="选择模拟报告" @change="open"><el-option v-for="r in reports" :key="r.evidence_id" :label="r.title" :value="r.evidence_id"/></el-select></p>
  <el-alert v-if="error" :title="error" type="error" :closable="false" style="margin-top:20px"/>
  <el-empty v-if="!busy&&!report&&!error" description="当前账号尚未导入模拟调研数据。请使用导入数据的演示账号登录。"/>
  <div v-if="report" class="research-content">
    <div class="goal-grid research-stats"><div class="goal-card"><strong>{{report.sources.length}} 份</strong><p>虚构岗位样本</p></div><div class="goal-card"><strong>{{report.skill_frequency.length}} 项</strong><p>能力维度 · 本人状态待验证</p></div><div class="goal-card"><strong>6 周 · 60—72 小时</strong><p>每周最多 12 小时为模拟假设</p></div></div>
    <el-tabs v-model="tab">
      <el-tab-pane label="调研分析" name="overview">
        <section class="table-card research-section"><h2>本次方向与事实边界</h2><p>以 Agent 应用开发实习／初级岗位为模拟目标，将 joblearn 的职业数据场景与 CareerForge 的显式工作流结合，设计一个只读岗位学习助手。</p><h3>已确认的需求</h3><ul><li v-for="item in report.provenance.confirmed_user_preferences" :key="item">{{item}}</li></ul><h3>待确认的假设</h3><ul><li v-for="item in report.provenance.assumptions" :key="item">{{item}}</li></ul></section>
        <section class="table-card research-section"><h2>模拟岗位样本</h2><p class="muted">无真实企业或招聘链接。点击查看已经保存到岗位来源库的原文。</p><el-table :data="report.sources"><el-table-column prop="mock_id" label="样本" width="110"/><el-table-column prop="title" label="岗位方向" min-width="230"/><el-table-column label="材料"><template #default="{row}"><el-button link type="primary" @click="readSource(row)">查看模拟 JD 原文</el-button></template></el-table-column></el-table></section>
        <section class="table-card research-section"><h2>技能需求统计</h2><p class="muted">每份 JD 每项技能计数一次；必须项与加分项分开。百分比仅描述五份人为设定的样本。</p><el-table :data="report.skill_frequency"><el-table-column prop="skill" label="能力" min-width="210"/><el-table-column label="必须项"><template #default="{row}">{{row.must_count}} / {{row.sample_count}}</template></el-table-column><el-table-column label="样本出现率" min-width="150"><template #default="{row}"><el-progress :percentage="Math.round(row.must_ratio*100)"/></template></el-table-column><el-table-column prop="preferred_count" label="加分项岗位数"/></el-table></section>
      </el-tab-pane>
      <el-tab-pane label="个人材料与缺口" name="alignment">
        <section v-for="e in report.personal_evidence" :key="e.id" class="table-card research-section"><h2>{{e.project}}</h2><p>{{e.basis}} · 本人贡献与掌握程度待验证</p><h3>已有材料描述</h3><ul><li v-for="item in e.observed_claims" :key="item">{{item}}</li></ul><h3>不能据此证明</h3><ul><li v-for="item in e.does_not_prove" :key="item">{{item}}</li></ul></section>
        <section class="table-card research-section"><h2>能力对齐与补证方向</h2><el-table :data="report.alignment"><el-table-column prop="skill" label="能力" min-width="200"/><el-table-column prop="priority" label="模拟优先级" width="110"/><el-table-column label="本人状态" width="100"><template #default>待验证</template></el-table-column><el-table-column prop="reason" label="分析依据" min-width="220"/><el-table-column prop="next_evidence" label="下一份证据" min-width="220"/></el-table></section>
      </el-tab-pane>
      <el-tab-pane label="六周学习计划" name="plan">
        <section class="table-card research-section"><h2>计划草稿 · 尚未采纳</h2><p>估计总投入 60—72 小时；每周 12 小时尚未由你确认。下面保留本次导入的原始草稿；后续编辑和采纳请进入计划工作区。</p><router-link v-if="planAvailable" :to="{path:'/planning',query:{plan:report.plan_id}}"><el-button type="primary">打开可编辑的学习计划</el-button></router-link><el-alert v-else title="关联计划当前不可访问；以下只展示报告内原始模拟安排。" type="info" :closable="false"/></section>
        <section v-for="w in report.weeks" :key="w.id" class="table-card research-section"><div class="card-top"><h2>{{w.id}} · {{w.title}}</h2><el-tag>{{w.effort_hours.min}}—{{w.effort_hours.max}} 小时</el-tag></div><p>先修：{{w.prerequisites.join('、') || '无'}} · 当前未开始，验收待验证</p><h3>学习与实践</h3><ul><li v-for="a in w.activities" :key="a">{{a}}</li></ul><h3>交付物</h3><p>{{w.deliverables.join('、')}}</p><el-collapse><el-collapse-item title="查看完整验收规则"><div v-for="(c,n) in w.criteria" :key="n"><h4>{{c.dimension==='explanation'?'本人解释':'成果表现'}}：{{c.scenario}}</h4><p>通过：{{c.pass_rule}}</p><p>失败：{{c.fail_rule}}</p><p>未知：{{c.unknown_rule}}</p></div></el-collapse-item></el-collapse></section>
      </el-tab-pane>
      <el-tab-pane label="实践与评测" name="evaluation"><section class="table-card research-section"><h2>{{report.practice_mock.title}}</h2><ul><li v-for="c in report.practice_mock.conditions" :key="c">{{c}}</li></ul><p>{{report.practice_mock.note}}</p></section><section class="table-card research-section"><h2>离线评测设计 · 尚未运行</h2><p>{{report.evaluation.synthetic_question_target}} 条合成问题：{{report.evaluation.development_count}} 条开发、{{report.evaluation.frozen_validation_count}} 条冻结验证。</p><ul><li v-for="m in report.evaluation.metrics" :key="m">{{m}}</li></ul><p>覆盖可回答、缺证据、冲突和越权；结果、准确率及耗时均未填写，不以模拟数据证明真实效果。</p></section></el-tab-pane>
      <el-tab-pane label="完整报告" name="full"><section class="table-card research-section"><h2>完整模拟调研报告</h2><pre class="research-report">{{report.report_text}}</pre></section></el-tab-pane>
    </el-tabs>
    <p class="muted">报告日期：{{report.created_at}} · 数据集：{{report.dataset_id}} · 仅当前账号可读取</p>
  </div>
  <el-dialog v-model="sourceDialog" :title="sourceTitle" width="min(850px,96vw)"><el-alert v-if="sourceError" :title="sourceError" type="error" :closable="false"/><p v-if="sourceBusy">正在读取来源…</p><pre v-else-if="sourceText" class="research-report">{{sourceText}}</pre></el-dialog>
</template>
<style scoped>
.research-content{margin-top:24px}.research-stats{margin-bottom:28px}.research-stats strong{font-size:21px;color:#285c50}.research-stats .goal-card{padding:24px}.research-section{padding:24px;margin:0 0 22px}.research-section h2{font-size:19px;margin:8px 0 16px}.research-section h3{font-size:15px;margin-top:23px}.research-section p,.research-section li{line-height:1.9;font-size:14px}.research-report{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/2 inherit;margin:0}.research-section ul{padding-left:22px}
</style>
