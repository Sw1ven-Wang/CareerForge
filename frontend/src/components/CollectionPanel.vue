<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, mutation } from '../api/client'
import RunPanel from './RunPanel.vue'

const props = defineProps<{ jobCategory?: string }>()
const emit = defineEmits<{ imported: [] }>()
type Portal = { id: string; name: string; home: string; search_url: string; keyword_query: boolean; query_scope: string; detail_adapter: boolean }
type Config = { channels: Portal[]; model: string; agent_available: boolean; search_api_configured: boolean }
type Observation = { status: string; title?: string; url?: string; channel?: string; message?: string; source_id?: string; candidates?: {title: string; url: string}[]; browser_url?: string; request_trace?: {url: string; method?: string; status: number}[] }
type Result = { run_id: string; status: string; query: string; mode: string; observations: Observation[]; http_requests: number; request_limit: number; model_calls: number; model_call_limit: number }
type History = { run_id: string; status: string; query: string; mode: string; created_at: string }
const config = ref<Config | null>(null), history = ref<History[]>([]), result = ref<Result | null>(null)
const dialog = ref(false), busy = ref(false), runId = ref(''), skill = ref(''), showSkill = ref(false)
const write = mutation()
const form = reactive({ query: 'Agent', channels: ['nowcoder', 'tencent'], mode: 'manual' as 'manual'|'agent', job_category: 'Agent 开发', limit: 6, model_call_limit: 2, allow_model_calls: false, allow_search_api: false, refresh_existing: false, urls: '' })
const terminal = new Set(['succeeded','partially_succeeded','failed','cancelled','budget_stopped'])
const active = computed(() => !!result.value && !terminal.has(result.value.status))
const statusNames: Record<string,string> = {collected:'已采集',reused:'复用已有正文',searched:'查询完成',browser_search_required:'需浏览器查询',robots_denied:'路径禁止自动采集',access_restricted:'访问受限',browser_required:'需浏览器读取',request_budget:'请求额度已用尽',timeout:'读取超时',schema_changed:'页面结构变化',parse_error:'解析失败',revoked:'已撤销',network_error:'网络不可用'}
async function loadHistory() { history.value = await api<History[]>('/collection/runs') }
async function refresh() {
  if (!runId.value) return
  const previous = result.value?.status
  result.value = await api<Result>('/collection/runs/' + runId.value)
  if (terminal.has(result.value.status) && previous !== result.value.status) { emit('imported'); await loadHistory() }
}
async function changed() { try { await refresh() } catch (error) { ElMessage.error(String(error)) } }
onMounted(async () => {
  try {
    config.value = await api<Config>('/collection/config')
    await loadHistory()
    if (history.value[0]) { runId.value = history.value[0].run_id; await refresh() }
  } catch (error) { ElMessage.error(String(error)) }
})
function capability(portal: Portal) { return portal.keyword_query ? '自动查询' + (portal.query_scope ? ' · ' + portal.query_scope : '') : portal.detail_adapter ? '仅详情链接采集' : '需浏览器协助' }
function open() { form.job_category = props.jobCategory || 'Agent 开发'; dialog.value = true }
async function start() {
  if (form.mode === 'agent' && !form.allow_model_calls) { ElMessage.warning('请先开启本轮模型调用'); return }
  if (!form.urls.trim() && !form.allow_search_api && !config.value?.channels.some(portal => form.channels.includes(portal.id) && portal.keyword_query)) { ElMessage.warning('所选渠道暂不支持直接查询，请粘贴详情链接或选择腾讯 / 牛客校招'); return }
  busy.value = true
  try {
    const payload = { ...form, query: form.query.trim(), job_category: form.job_category.trim(), request_limit: 30,
      allow_model_calls: form.mode === 'agent' && form.allow_model_calls, urls: form.urls.split(/\r?\n/).map(url => url.trim()).filter(Boolean) }
    const created = await write<{run_id:string}>('/collection/runs','POST',payload)
    runId.value = created.run_id; result.value = null; dialog.value = false
    await refresh(); await loadHistory()
    ElMessage.success('调研采集任务已创建')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '创建失败') }
  finally { busy.value = false }
}
async function choose(id: string) { runId.value = id; result.value = null; await changed() }
async function viewSkill() {
  try { skill.value = (await api<{content:string}>('/collection/skill')).content; showSkill.value = true }
  catch (error) { ElMessage.error(String(error)) }
}
function browse(portal: Portal) { return portal.search_url.replace(/Agent/g, encodeURIComponent(form.query.trim() || 'Agent')) }
</script>
<template>
  <section class="collection-card">
    <div class="collection-heading"><div><h2>联网岗位调研</h2><p>手动查询、粘贴详情链接采集，或让 Agent 使用同一套调研工具。</p></div><div><el-button @click="viewSkill">查看官网调研 Skill</el-button><el-button type="primary" :disabled="active" @click="open">＋ 查询 / 采集岗位</el-button></div></div>
    <div v-if="history.length" class="collection-history"><span>采集记录</span><el-select :model-value="runId" @change="choose"><el-option v-for="item in history" :key="item.run_id" :value="item.run_id" :label="item.query + ' · ' + (item.mode === 'agent' ? 'Agent' : '手动') + ' · ' + new Date(item.created_at).toLocaleString()"/></el-select></div>
    <template v-if="result">
      <p class="muted">本轮网络请求 {{ result.http_requests }} / {{ result.request_limit }} · 模型调用 {{ result.model_calls }} / {{ result.model_call_limit }}</p>
      <div class="collection-results">
      <div v-for="(item,index) in result.observations" :key="index" class="collection-observation">
        <el-tag :type="['collected','reused'].includes(item.status) ? 'success' : item.status === 'searched' ? 'info' : 'warning'">{{ statusNames[item.status] || item.status }}</el-tag>
        <strong>{{ item.title || config?.channels.find(portal => portal.id === item.channel)?.name || '岗位详情' }}</strong>
        <p v-if="item.message">{{ item.message }}</p>
        <a v-if="item.url || item.browser_url" :href="item.url || item.browser_url" target="_blank" rel="noopener noreferrer">查看原始页面 ↗</a>
        <p v-if="item.source_id" class="muted">已进入下方岗位目录，可打开详情继续标准化核对。</p>
        <ul v-if="item.candidates?.length"><li v-for="candidate in item.candidates" :key="candidate.url"><a :href="candidate.url" target="_blank" rel="noopener noreferrer">{{ candidate.title }}</a></li></ul>
        <details v-if="item.request_trace?.length"><summary>采集路径</summary><p v-for="(step,n) in item.request_trace" :key="n" class="collection-url">{{ step.method || 'GET' }} {{ step.status }} · {{ step.url }}</p></details>
      </div>
      </div>
      <details><summary>后台运行记录</summary><RunPanel v-if="runId" :key="runId" :run-id="runId" @changed="changed"/></details>
    </template>
  </section>
  <el-dialog v-model="dialog" title="联网查询与岗位采集" width="720px">
    <el-form label-position="top">
      <el-form-item label="查询关键词 / 调研指令"><el-input v-model="form.query" maxlength="200" placeholder="例如：Agent 开发、智能体工程师"/></el-form-item>
      <el-form-item label="查询与采集渠道"><el-checkbox-group v-model="form.channels"><el-checkbox v-for="portal in config?.channels" :key="portal.id" :value="portal.id">{{ portal.name }}（{{ capability(portal) }}）</el-checkbox></el-checkbox-group></el-form-item>
      <el-form-item label="执行方式"><el-radio-group v-model="form.mode"><el-radio value="manual">手动查询 / 采集</el-radio><el-radio value="agent" :disabled="!config?.agent_available">Agent 自主调研</el-radio></el-radio-group></el-form-item>
      <el-alert type="info" :closable="false" title="腾讯、牛客校招支持直接关键词查询；其他渠道未接通直接查询。牛客、猎聘、百度、腾讯支持详情链接采集；跨站发现需另配置搜索服务。受限页面会保留原因，不会冒充完整 JD。"/>
      <div class="collection-portals"><a v-for="portal in config?.channels.filter(item => form.channels.includes(item.id))" :key="portal.id" :href="browse(portal)" target="_blank" rel="noopener noreferrer">打开{{ portal.name }} ↗</a></div>
      <el-form-item label="详情链接（可选，每行一条，最多12条；填写后手动模式优先采集这些链接）"><el-input v-model="form.urls" type="textarea" :rows="4" placeholder="https://www.nowcoder.com/jobs/detail/…"/></el-form-item>
      <el-form-item label="保存到岗位分类"><el-input v-model="form.job_category" maxlength="100"/></el-form-item>
      <el-form-item label="本轮岗位上限"><el-input-number v-model="form.limit" :min="1" :max="12"/></el-form-item>
      <el-checkbox v-model="form.refresh_existing">重新抓取已有岗位并保存新版本</el-checkbox>
      <el-checkbox v-model="form.allow_search_api" :disabled="!config?.search_api_configured">启用本轮跨站搜索服务（可能计费）</el-checkbox>
      <template v-if="form.mode === 'agent'"><el-form-item label="模型调用上限"><el-input-number v-model="form.model_call_limit" :min="1" :max="3"/></el-form-item><el-checkbox v-model="form.allow_model_calls">开启本轮真实模型调用：{{ config?.model }} low，每次最多 2048 输出 token；供应商费用尚未定价</el-checkbox></template>
    </el-form>
    <template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" :loading="busy" @click="start">{{ form.mode==='agent' ? '启动 Agent 调研' : '开始查询 / 采集' }}</el-button></template>
  </el-dialog>
  <el-dialog v-model="showSkill" title="嵌入式官网调研 Skill" width="760px"><pre class="collection-skill">{{ skill }}</pre></el-dialog>
</template>
<style scoped>
.collection-card{background:#fff;border:1px solid #e2e8eb;border-radius:12px;padding:22px;margin-bottom:26px}
.collection-heading{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}
.collection-heading h2{font-size:18px;margin:0 0 10px}.collection-heading p{font-size:13px;color:#7a878b;margin:0;line-height:1.8}
.collection-history{display:flex;align-items:center;gap:14px;margin-top:20px}.collection-history .el-select{flex:1}
.collection-results{max-height:420px;overflow:auto;margin-bottom:16px}
.collection-observation{border-top:1px solid #e8eeeb;margin-top:16px;padding-top:16px;font-size:13px;line-height:1.8}
.collection-observation strong{margin-left:12px}.collection-observation a,.collection-portals a{color:#247968;text-decoration:underline}
.collection-portals{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 20px;font-size:13px}
.collection-url{overflow-wrap:anywhere;color:#7a878b;font-size:11px}.collection-skill{white-space:pre-wrap;line-height:1.9;font-family:inherit;font-size:13px}
</style>
