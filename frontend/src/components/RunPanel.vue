<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, mutation } from '../api/client'
import { followRun, type RunEvent } from '../api/runEvents'
import type { Run } from '../stores/runs'
const props = defineProps<{ runId: string }>()
const emit = defineEmits<{ changed: [] }>()
const run = ref<Run | null>(null), events = ref<RunEvent[]>([]), error = ref(''), busy = ref(false)
const write = mutation()
let controller: AbortController | undefined
const names: Record<string, string> = { queued: '排队中', running: '执行中', waiting_user: '待你确认', waiting_children:'等待子任务',
  succeeded: '已完成', partially_succeeded: '部分完成', failed: '失败', budget_stopped: '预算已用尽', cancelled: '已取消' }
const eventNames: Record<string, string> = {
  'run.created': '任务已创建', 'run.started': '开始执行', 'stage.completed': '阶段结果已保存',
  'run.waiting_user': '等待你的决定', 'wait.requested': '已保存待确认问题',
  'run.resumed': '已接受决定，准备继续', 'run.recovering': '正在恢复执行',
  'learning.result_recovered': '已修复保存结果，原始失败记录保留',
  'knowledge.search': '已检索知识库', 'knowledge.read': '已读取代码原文', 'knowledge.related': '已查询结构候选',
  'task.created':'子任务已建立','budget.reserved':'子任务额度已预留','budget.consumed':'调用已计入总消耗','budget.settled':'未用额度已释放','run.children_ready':'子任务结束，准备统一校验','task.joined':'子任务结果已汇总','stage.failed': '阶段失败，未自动重试',
  'run.finished': '本次运行已结束', 'run.cancelled': '运行已取消',
}
const failureNames: Record<string,string> = {
  INVALID_LEARNING_EVIDENCE: '学习说明格式或原文引用未通过校验',
  PROVIDER_OUTPUT_OVER_LIMIT: '供应商返回用量超过请求上限，已停止后续调用',
  MODEL_RESULT_UNCERTAIN: '上次调用结果未保存，已停止自动重试',
  rejected: '模型供应商拒绝了请求', authentication: '模型服务认证失败',
  timeout: '模型请求超时，未自动重试', output_limit: '模型输出不完整，未发布说明',
}
async function refresh() { run.value = await api<Run>(`/runs/${props.runId}`) }
watch(() => props.runId, async id => {
  controller?.abort(); controller = new AbortController(); events.value = []; error.value = ''
  try { await refresh() } catch (e) { error.value = String(e); return }
  void followRun(id, event => {
    events.value.push(event)
    if (event.type.startsWith('run.') || event.type === 'stage.completed' || event.type.startsWith('budget.') || event.type.startsWith('task.')) {
      void refresh().catch(e => { error.value = String(e) }); emit('changed')
    }
  }, message => { error.value = message }, controller.signal)
}, { immediate: true })
onBeforeUnmount(() => controller?.abort())
async function choose(choice: string) {
  if (!run.value?.waiting_input) return
  busy.value = true
  try {
    await write(`/runs/${run.value.run_id}/resume`, 'POST', {
      expected_version: run.value.version, wait_request_id: run.value.waiting_input.wait_request_id,
      input_manifest_hash: run.value.input_manifest_hash, response: { choice },
    }); await refresh(); emit('changed')
  } catch (e) { ElMessage.error(String(e)) } finally { busy.value = false }
}
async function cancel() {
  if (!run.value) return
  busy.value = true
  try {
    await api(`/runs/${run.value.run_id}/cancel`, 'POST', { expected_version: run.value.version })
    await refresh(); emit('changed')
  } catch (e) { ElMessage.error(String(e)) } finally { busy.value = false }
}
</script>
<template>
  <section v-if="run" class="surface" style="padding:24px">
    <el-tag>{{ names[run.status] ?? run.status }}</el-tag>
    <el-tag v-if="run.workflow_version.startsWith('s02-demo-')" type="warning">合成流程演示</el-tag>
    <h2>运行记录</h2><p>关闭页面后任务仍会继续。重新登录可查看进度。</p>
    <p>已消耗 {{ run.usage.calls_reserved }} / {{ run.usage.call_limit }} 次调用额度 · 未用预留 {{run.usage.held_calls??0}}</p>
    <section v-if="run.family?.children.length"><h3>子任务与预算结算</h3><p>{{run.family.accounting_note}}</p><article v-for="child in run.family.children" :key="child.run_id" style="margin:12px 0;padding:10px;border:1px solid #dce5dc"><router-link :to="'/runs/'+child.run_id">审核子任务 {{child.run_id.slice(0,8)}}</router-link> · {{names[child.status]??child.status}}<p>分配 {{child.allocated_calls}} · 消耗 {{child.consumed_calls}} · 未用预留 {{child.held_calls}} · 已释放 {{child.released_calls}}</p><small v-for="(receipt,i) in child.usage" :key="i">收据 {{i+1}} · {{receipt.status}} · 输入 {{receipt.usage?.input_tokens??'未知'}} / 输出 {{receipt.usage?.output_tokens??'未知'}}<br></small></article></section>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <section v-if="run.waiting_input">
      <p v-if="run.waiting_input.question.draft?.draft">{{ run.waiting_input.question.draft.draft }}</p>
      <h3>{{ run.waiting_input.question.text ?? '请补充本次决定' }}</h3>
      <el-button v-for="choice in run.waiting_input.response_schema.properties.choice.enum" :key="choice"
        :loading="busy" @click="choose(choice)">{{ ({accept: '接受草稿', reject: '拒绝草稿'} as Record<string,string>)[choice] ?? choice }}</el-button>
    </section>
    <el-button v-if="['queued','running','waiting_user','waiting_children'].includes(run.status)" :disabled="busy" @click="cancel" style="margin-top:16px">取消运行</el-button>
    <h3>已保存的进度</h3>
    <ol style="list-style:none;padding-left:0"><li v-for="event in events" :key="`${event.run_id}:${event.seq}`">{{ event.seq }} · {{ eventNames[event.type] ?? '进度已更新' }} <span v-if="event.type==='stage.failed'">：{{failureNames[String(event.payload.error)] ?? '本阶段未完成，请检查输入或模型配置'}}</span> <small>{{ new Date(event.created_at).toLocaleTimeString() }}</small></li></ol>
    <p v-if="!events.length">尚无可显示的事件。</p>
  </section>
</template>
