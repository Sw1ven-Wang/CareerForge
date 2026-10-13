<script setup lang="ts">
import { computed } from 'vue'
export type LearningPoint = { title: string; explanation: string; citation_id: string; quote: string; path: string; commit: string; quote_start_line: number; quote_end_line: number }
export type LearningReport = {
  id: string; kind: string; status: string; objective_title?: string; objective_version_id?: string; instruction?: string; run_id?: string; synthetic?: boolean; generated_by?: string
  guide?: { status: string; summary: string; points: LearningPoint[]; reading_order: string[]; practice: string[]; questions: string[]; limitations: string[] }
  sources?: { citation_id: string; path: string; start_line: number; end_line: number; commit: string; context_truncated?: boolean }[]
  trace?: { tool: string; arguments: Record<string,unknown>; status: string; candidate_count: number; path?: string; start_line?: number; end_line?: number }[]
  compatibility_repairs?: {field:string;action:string}[]; recovery?: {method:string;original_status:string;extra_model_calls:number}; budget_warning?: {requested_output_tokens:number;reported_output_tokens:number}
  usage?: { calls?: number; input_tokens: number|null; output_tokens: number|null; status?: string }
}
const props = defineProps<{ report: LearningReport }>()
const emit = defineEmits<{ open: [id: string]; point: [point: LearningPoint]; run: [id: string] }>()
const sourceLabels = computed(() => Object.fromEntries((props.report.sources ?? []).map(s => [s.citation_id, `${s.path}:${s.start_line}–${s.end_line}`])))
const toolNames: Record<string,string> = { search_code: '检索代码', read_code: '读取原文', related_code: '追踪结构候选' }
const states: Record<string,string> = { queued: '排队中', running: '检索与分析中', cancelled: '已取消', failed: '未生成说明', budget_stopped: '额度已用尽', unavailable: '依据不可用' }
</script>
<template>
  <section class="learning-explanation">
    <div class="report-heading"><strong>学习说明</strong><el-tag v-if="report.synthetic" size="small" type="warning">离线模拟</el-tag><el-tag v-if="report.guide" size="small" :type="report.guide.status==='grounded'?'success':'info'">{{report.guide.status==='grounded'?'原文依据已校验':'依据不足'}}</el-tag><el-tag v-else size="small">{{states[report.status] ?? report.status}}</el-tag></div>
    <el-alert v-if="report.recovery" title="原任务格式校验失败；已使用保存结果补齐显示标题并重新核验原文，没有追加模型调用。" type="info" :closable="false"/><el-alert v-if="report.budget_warning" :title="`供应商报告输出 ${report.budget_warning.reported_output_tokens} token，超过请求上限 ${report.budget_warning.requested_output_tokens}；本次额度兼容性验证未通过。`" type="warning" :closable="false"/>
    <small v-if="report.compatibility_repairs?.length" class="muted">学习点标题由系统补齐，解释与原文摘录保留模型原始内容。</small>
    <p v-if="report.objective_title" class="objective-title">目标：{{report.objective_title}}</p>
    <p v-if="!report.guide">{{report.instruction}}</p>
    <template v-if="report.guide"><p class="summary">{{report.guide.summary}}</p>
      <article v-for="(point,i) in report.guide.points" :key="i" class="learning-point"><h4>{{i+1}}. {{point.title}}</h4><p>{{point.explanation}}</p><pre>{{point.quote}}</pre><el-button link @click="emit('point',point)">{{point.path}}:{{point.quote_start_line}}–{{point.quote_end_line}} · 定位原文</el-button><small>commit {{point.commit.slice(0,12)}}</small></article>
      <template v-if="report.guide.reading_order.length"><h4>建议阅读顺序</h4><ol><li v-for="id in report.guide.reading_order" :key="id"><el-button link @click="emit('open',id)">{{sourceLabels[id] ?? '查看代码依据'}}</el-button></li></ol></template>
      <template v-if="report.guide.practice.length"><h4>迁移练习</h4><ul><li v-for="item in report.guide.practice" :key="item">{{item}}</li></ul></template>
      <template v-if="report.guide.questions.length"><h4>自检问题</h4><ul><li v-for="item in report.guide.questions" :key="item">{{item}}</li></ul></template>
      <template v-if="report.guide.limitations.length"><h4>范围与未知</h4><ul><li v-for="item in report.guide.limitations" :key="item">{{item}}</li></ul></template>
      <p class="muted">仅静态阅读，解释仍需核对；未运行项目或确认学习目标完成。</p><p v-if="report.sources?.some(s=>s.context_truncated)" class="muted">部分读取受上下文预算限制，引用仅覆盖实际读取范围。</p>
    </template>
    <details v-if="report.trace?.length"><summary>检索轨迹 · {{report.trace.length}} 次工具操作</summary><ol><li v-for="(step,i) in report.trace" :key="i"><strong>{{toolNames[step.tool] ?? step.tool}}</strong> · {{step.status}}<p v-if="step.arguments.query">{{step.arguments.query}}</p><small v-if="step.path">{{step.path}}:{{step.start_line}}–{{step.end_line}}</small><small v-else>候选 {{step.candidate_count}}</small></li></ol></details>
    <small v-if="report.usage" class="muted">模型调用 {{report.usage.calls}} 次 · token {{report.usage.input_tokens ?? '未知'}} / {{report.usage.output_tokens ?? '未知'}} · 金额未定价</small>
    <el-button v-if="report.run_id" link @click="emit('run',report.run_id)">查看此说明的运行记录</el-button>
  </section>
</template>
<style scoped>
.report-heading{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.objective-title{font-weight:600;color:#38644f}.summary,.learning-point p,li{line-height:1.8;white-space:pre-wrap}.learning-point{border-top:1px solid #e4eae6;padding:8px 0;margin-top:8px}.learning-point h4{margin:8px 0}.learning-point pre{white-space:pre-wrap;background:#edf3ef;padding:10px;border-left:3px solid #5a8b70;border-radius:4px;font:12px/1.7 Consolas,monospace;max-height:180px;overflow:auto}.learning-point small,.muted,details small{display:block;font-size:12px;color:#74847d;line-height:1.8}.learning-point .el-button,li .el-button{max-width:100%;white-space:normal;text-align:left;height:auto;line-height:1.7}h4{font-size:13px;margin:16px 0 8px}ol,ul{padding-left:20px}details{margin:12px 0;background:#f4f7f5;padding:10px;border-radius:6px}summary{cursor:pointer}details p{font-size:12px;overflow-wrap:anywhere}
</style>
