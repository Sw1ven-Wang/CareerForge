<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, mutation } from '../api/client'

export type KnowledgeHit = {
  chunk_id: string; repository_snapshot_id: string; commit: string; path: string; symbol: string
  kind: string; start_line: number; end_line: number; content: string; matched_terms?: string[]
  match_reason?: string; project_name?: string; parse_status?: string; relation?: string
}
const props = defineProps<{ snapshotId?: string; objectiveId?: string; objectiveTitle?: string }>()
const emit = defineEmits<{ open: [citationId: string]; bound: [] }>()
const write = mutation()
const query = ref(''), results = ref<KnowledgeHit[]>([]), selected = ref<KnowledgeHit|null>(null)
const related = ref<KnowledgeHit[]>([]), busy = ref(false), actionBusy = ref(false), searched = ref(false)
const aliases = ref<string[]>([]), note = ref(''), message = ref('')
const lines = computed(() => selected.value?.content.replace(/\n$/, '').split('\n').map((text, i) => ({ text, number: selected.value!.start_line + i })) ?? [])
watch(() => props.objectiveTitle, title => { if (title) query.value = title }, { immediate: true })
watch(() => props.snapshotId, () => { results.value = []; selected.value = null; searched.value = false })
async function search() {
  if (!query.value.trim()) return
  busy.value = true; message.value = ''; selected.value = null; related.value = []
  try {
    const data = await api<{ results: KnowledgeHit[]; expanded_topics: string[] }>('/knowledge/search?q=' + encodeURIComponent(query.value) + (props.snapshotId ? '&snapshot_id=' + props.snapshotId : ''))
    results.value = data.results; aliases.value = data.expanded_topics; searched.value = true
    if (results.value[0]) await preview(results.value[0])
  } catch (e) { ElMessage.error(String(e)) } finally { busy.value = false }
}
async function preview(hit: KnowledgeHit) {
  selected.value = hit; related.value = []
  const id = hit.chunk_id
  try {
    const data = await api<{ results: KnowledgeHit[] }>('/knowledge/chunks/' + id + '/related')
    if (selected.value?.chunk_id === id) related.value = data.results
  } catch (e) { ElMessage.error(String(e)) }
}
async function open() {
  if (!selected.value) return
  actionBusy.value = true
  try {
    const c = await write<{ citation_id: string }>('/knowledge/chunks/' + selected.value.chunk_id + '/citation', 'POST', {})
    emit('open', c.citation_id)
  } catch (e) { ElMessage.error(String(e)) } finally { actionBusy.value = false }
}
async function bind() {
  if (!selected.value || !props.objectiveId) return
  actionBusy.value = true
  try {
    await write('/knowledge/links', 'POST', { chunk_id: selected.value.chunk_id, objective_version_id: props.objectiveId, note: note.value })
    message.value = '已关联到当前学习目标；可在学习工作台直接打开。'; emit('bound'); ElMessage.success(message.value)
  } catch (e) { ElMessage.error(String(e)) } finally { actionBusy.value = false }
}
onMounted(() => { if (!query.value) query.value = '工具调用' })
defineExpose({ search })
</script>
<template>
  <div class="knowledge-search">
    <div class="search-bar"><el-input v-model="query" placeholder="搜索学习主题、函数名或代码，例如 工具调用 / checkpoint / retry" clearable @keyup.enter="search"/><el-button type="primary" :loading="busy" @click="search">检索代码</el-button></div>
    <p class="muted">结构与全文检索 · 未启用向量或模型调用<span v-if="aliases.length"> · 主题别名：{{ aliases.join('、') }}</span></p>
    <el-empty v-if="searched && !results.length" description="未找到依据。请尝试函数名、英文关键词，或为项目补充主题标签。"/>
    <div v-if="results.length" class="results-layout" v-loading="busy">
      <div class="hits"><button v-for="hit in results" :key="hit.chunk_id" class="hit" :class="{ active: selected?.chunk_id === hit.chunk_id }" @click="preview(hit)"><strong>{{ hit.symbol }}</strong><span>{{ hit.project_name }} · {{ hit.path }}:{{ hit.start_line }}–{{ hit.end_line }}</span><small>{{ hit.kind === 'file_window' ? '文件文本窗口' : 'Python 结构定义' }} · {{ hit.match_reason }}</small><small>命中：{{ hit.matched_terms?.join('、') || '项目主题' }}</small></button></div>
      <div v-if="selected" class="preview">
        <div class="preview-heading"><strong>{{ selected.path }}:{{ selected.start_line }}–{{ selected.end_line }}</strong><el-button :loading="actionBusy" @click="open">在学习工作台打开</el-button></div>
        <small class="muted">固定 commit {{ selected.commit }} · {{ selected.parse_status }}</small>
        <div class="source"><div v-for="line in lines" :key="line.number"><span>{{ line.number }}</span><code>{{ line.text || ' ' }}</code></div></div>
        <template v-if="objectiveId"><p>关联目标：{{ objectiveTitle || objectiveId }}</p><el-input v-model="note" placeholder="学习这段实现的原因（可选）" :maxlength="2000"/><el-button type="primary" :loading="actionBusy" @click="bind">关联到当前学习目标</el-button></template>
        <el-alert v-if="message" :title="message" type="success" :closable="false"/>
        <div v-if="related.length" class="related"><h4>相关结构与引用候选</h4><p class="muted">静态包含或同名引用候选，可能存在歧义，不证明运行时调用。</p><el-button v-for="hit in related" :key="hit.chunk_id" link @click="preview(hit)">{{ hit.symbol }} · {{ hit.path }}:{{ hit.start_line }} · {{ hit.relation === 'contains' ? '包含' : '引用候选' }}</el-button></div>
      </div>
    </div>
  </div>
</template>
<style scoped>
.search-bar{display:flex;gap:10px}.muted{font-size:12px;color:#74847d;line-height:1.8;overflow-wrap:anywhere}.results-layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:16px}.hits{max-height:720px;overflow:auto}.hit{display:block;width:100%;text-align:left;border:1px solid #dce5e0;background:white;border-radius:8px;padding:12px;margin-bottom:8px;cursor:pointer;overflow-wrap:anywhere}.hit strong,.hit span,.hit small{display:block;line-height:1.7}.hit span,.hit small{font-size:12px;color:#73837c}.hit.active{border-color:#579278;background:#edf5ef}.preview{min-width:0}.preview-heading{display:flex;justify-content:space-between;align-items:center;gap:10px;overflow-wrap:anywhere}.source{max-height:510px;overflow:auto;background:#162522;color:#e5ede8;border-radius:8px;padding:12px 0;margin:10px 0;font:13px/1.8 Consolas,monospace}.source>div{display:flex;min-width:max-content}.source span{width:52px;text-align:right;flex-shrink:0;padding-right:16px;color:#b1c7ba}.source code{white-space:pre;padding-right:20px}.preview>.el-button,.preview>.el-alert{margin-top:10px}.related .el-button{display:block;max-width:100%;height:auto;white-space:normal;text-align:left;margin:8px 0}@media(max-width:1050px){.results-layout{grid-template-columns:1fr}.hits{max-height:240px}.preview-heading{flex-wrap:wrap}}
</style>
