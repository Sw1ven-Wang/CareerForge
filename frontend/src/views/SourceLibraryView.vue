<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, mutation, uploadAttachment } from '../api/client'
import type { components } from '../types/generated_api'
type SourceImport = components['schemas']['SourceImport']
type Row = { source_id: string; source_type: string; title: string | null; version: number; read_status: string }
type Snapshot = { source_snapshot_id: string; version_no: number; metadata: Record<string, unknown> }
type Detail = Row & { source_url: string | null; snapshots: Snapshot[] }
type SnapshotDetail = { content: string; metadata: Record<string, unknown>; content_hash: string; version_no: number }
const labels: Record<string, string> = { job_posting: '岗位 JD', interview_experience: '面经', official_doc: '官方资料', user_note: '个人笔记' }
const rows = ref<Row[]>([]), showImport = ref(false), busy = ref(false), filter = ref('')
const detail = ref<Detail | null>(null), selectedSnapshot = ref(''), snapshot = ref<SnapshotDetail | null>(null)
const correcting = ref(false), correction = reactive({ content: '', reason: '', source_type: '' })
const form = reactive({ source_type: 'job_posting' as SourceImport['source_type'], title: '', source_url: '', import_mode: 'text' as SourceImport['import_mode'], content: '' })
const attachmentFile = ref<File | null>(null), attachmentKey = ref(crypto.randomUUID())
function selectAttachment(event: Event) { attachmentFile.value = (event.target as HTMLInputElement).files?.[0] ?? null; attachmentKey.value = crypto.randomUUID() }
const saveMutation = mutation(), reviseMutation = mutation(), deleteMutation = mutation()
async function load() { try { rows.value = await api('/sources' + (filter.value ? `?source_type=${filter.value}` : '')) } catch (error) { ElMessage.error(String(error)) } }
onMounted(load)
async function save() {
  busy.value = true
  try {
    let attachmentId: string | null = null
    if (form.import_mode === 'attachment') {
      if (!attachmentFile.value) throw new Error('请选择一个文本附件')
      attachmentId = (await uploadAttachment(attachmentFile.value, 'source', attachmentKey.value)).attachment_id
    }
    const payload: SourceImport = { ...form, attachment_id: attachmentId, content: form.import_mode === 'text' ? form.content : null,
      source_url: form.source_url || null, title: form.title || null, published_at: null, published_at_precision: 'unknown' }
    await saveMutation('/sources/import', 'POST', payload); showImport.value = false
    form.content = ''; form.title = ''; form.source_url = ''; await load(); ElMessage.success('资料已保存')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '导入失败') }
  finally { busy.value = false }
}
async function view(row: Row) {
  try {
    detail.value = await api(`/sources/${row.source_id}`); snapshot.value = null
    selectedSnapshot.value = detail.value?.snapshots[0]?.source_snapshot_id ?? ''
    if (selectedSnapshot.value) await readSnapshot()
  } catch (error) { ElMessage.error(String(error)) }
}
async function readSnapshot() {
  if (!detail.value || !selectedSnapshot.value) return
  try { snapshot.value = await api(`/sources/${detail.value.source_id}/snapshots/${selectedSnapshot.value}`) }
  catch (error) { ElMessage.error(String(error)) }
}
function correct() { correction.content = snapshot.value?.content ?? ''; correction.reason = ''; correction.source_type = detail.value?.source_type ?? 'job_posting'; correcting.value = true }
async function revise() {
  if (!detail.value) return
  busy.value = true
  try {
    await reviseMutation(`/sources/${detail.value.source_id}/snapshots`, 'POST', {
      expected_version: detail.value.version, content: correction.content, change_reason: correction.reason,
      metadata_overrides: { source_type: correction.source_type },
    }); correcting.value = false; await view(detail.value); await load(); ElMessage.success('新快照已保存，历史版本保持原内容')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '修订失败') }
  finally { busy.value = false }
}
async function remove(row: Row) {
  try {
    await ElMessageBox.confirm('删除后立即撤销正文与历史引用的访问。后台清理状态可在删除记录中查询。', '删除资料', { type: 'warning' })
    const result = await deleteMutation<{ deletion_id: string; status: string }>(`/sources/${row.source_id}`, 'DELETE', { expected_version: row.version })
    detail.value = null; snapshot.value = null; await load(); ElMessage.success(`访问已撤销，清理状态：${result.status}`)
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : '删除失败') }
}
</script>
<template>
  <section class="page-heading"><div><span class="eyebrow">SOURCE LIBRARY</span><h1>岗位资料与来源</h1><p>保存原文与版本，让后续每项分析都有据可查。</p></div><el-button type="primary" @click="showImport = true">＋ 导入资料</el-button></section>
  <div class="library-toolbar"><el-select v-model="filter" placeholder="全部资料类型" clearable @change="load"><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value" /></el-select><span>链接线索不计为已读正文</span></div>
  <section class="table-card"><el-table :data="rows" empty-text="还没有资料，先导入一份真实 JD。" @row-click="view"><el-table-column label="资料名称" min-width="260"><template #default="{ row }"><strong>{{ row.title || '未命名资料' }}</strong><small class="row-subtitle">{{ row.source_id.slice(0, 8) }}</small></template></el-table-column><el-table-column label="类型" width="140"><template #default="{ row }">{{ labels[row.source_type] }}</template></el-table-column><el-table-column label="正文状态" width="130"><template #default="{ row }"><el-tag :type="row.read_status === 'available' ? 'success' : 'warning'" effect="light">{{ row.read_status === 'available' ? '正文可读' : row.read_status === 'revoked' ? '正文已撤销' : '正文未读取' }}</el-tag></template></el-table-column><el-table-column prop="version" label="版本" width="80"/><el-table-column label="操作" width="160"><template #default="{ row }"><el-button link type="primary" @click.stop="view(row)">查看</el-button><el-button link type="danger" @click.stop="remove(row)">删除</el-button></template></el-table-column></el-table></section>
  <el-dialog v-model="showImport" title="导入来源资料" width="640px"><el-form label-position="top"><el-form-item label="资料类型"><el-select v-model="form.source_type"><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value"/></el-select></el-form-item><el-form-item label="标题（可留空）"><el-input v-model="form.title"/></el-form-item><el-form-item label="原始链接（粘贴正文时可留空）"><el-input v-model="form.source_url" placeholder="https://…"/></el-form-item><el-form-item label="导入方式"><el-radio-group v-model="form.import_mode"><el-radio value="text">粘贴正文</el-radio><el-radio value="attachment">文本附件</el-radio><el-radio value="link_clue">只保存链接线索</el-radio></el-radio-group></el-form-item><el-form-item v-if="form.import_mode === 'text'" label="原文（最多 100,000 字符）"><el-input v-model="form.content" type="textarea" :rows="10" :maxlength="100000" show-word-limit/></el-form-item><el-form-item v-else-if="form.import_mode === 'attachment'" label="UTF-8 文本附件（最多 1 MiB）"><input type="file" accept=".txt,.md,.json,.log,.py,.xml" @change="selectAttachment" /></el-form-item><el-alert v-else type="info" :closable="false" title="仅保存链接，不自动读取、不生成摘要，也不纳入 JD 统计。"/></el-form><template #footer><el-button @click="showImport = false">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存资料</el-button></template></el-dialog>
  <el-drawer :model-value="!!detail" title="来源与版本" size="min(720px, 100vw)" @close="detail = null"><template v-if="detail"><h2>{{ detail.title || '未命名资料' }}</h2><p class="muted">{{ detail.source_url || '手动导入 · 未提供原链接' }}</p><el-select v-model="selectedSnapshot" placeholder="尚无正文快照" @change="readSnapshot"><el-option v-for="item in detail.snapshots" :key="item.source_snapshot_id" :label="`快照 v${item.version_no} · ${item.source_snapshot_id.slice(0, 8)}`" :value="item.source_snapshot_id"/></el-select><el-button @click="correct">{{ snapshot ? '更正为新快照' : '补充正文' }}</el-button><template v-if="snapshot"><div class="snapshot-meta"><el-tag>{{ labels[String(snapshot.metadata.source_type)] }}</el-tag><span>发布时间：{{ snapshot.metadata.published_at || '未知' }}</span></div><pre class="source-body">{{ snapshot.content }}</pre><small class="hash">SHA-256 {{ snapshot.content_hash }}</small></template><el-empty v-else description="链接已保存，正文未读取。可以补贴正文后继续。"/></template></el-drawer>
  <el-dialog v-model="correcting" title="保存新的来源快照" width="620px"><el-form label-position="top"><el-form-item label="资料类型"><el-select v-model="correction.source_type"><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value"/></el-select></el-form-item><el-form-item label="正文"><el-input v-model="correction.content" type="textarea" :rows="10" :maxlength="100000"/></el-form-item><el-form-item label="更正原因"><el-input v-model="correction.reason"/></el-form-item></el-form><template #footer><el-button type="primary" :loading="busy" @click="revise">保存新快照</el-button></template></el-dialog>
</template>
