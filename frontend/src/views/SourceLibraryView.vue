<script setup lang="ts">
import CollectionPanel from '../components/CollectionPanel.vue'
import JobContractPanel from '../components/JobContractPanel.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiPage, mutation, uploadAttachment } from '../api/client'
import type { components } from '../types/generated_api'
type SourceImport = components['schemas']['SourceImport']
type Row = { source_id: string; source_type: string; title: string | null; version: number; read_status: string; source_url?: string | null; job_category?: string | null; source_category?: string | null; library_category_id?: string | null }
type Snapshot = { source_snapshot_id: string; version_no: number; metadata: Record<string, unknown> }
type Detail = Row & { source_url: string | null; snapshots: Snapshot[] }
type SnapshotDetail = { content: string; metadata: Record<string, unknown>; content_hash: string; version_no: number }
const labels: Record<string, string> = { job_posting: '岗位 JD', interview_experience: '面经', official_doc: '官方资料', user_note: '个人笔记' }
const rows = ref<Row[]>([]), showImport = ref(false), busy = ref(false), filter = ref('')
const detail = ref<Detail | null>(null), selectedSnapshot = ref(''), snapshot = ref<SnapshotDetail | null>(null)
const correcting = ref(false), correction = reactive({ content: '', reason: '', source_type: '' })
const form = reactive({ source_type: 'job_posting' as SourceImport['source_type'], title: '', source_url: '', import_mode: 'text' as SourceImport['import_mode'], content: '' })
const jobCategory = ref(''), sourceCategory = ref('')
const provider = ref('manual_import'), boardScope = ref(''), employer = ref('')
const attachmentFile = ref<File | null>(null), attachmentKey = ref(crypto.randomUUID())
function selectAttachment(event: Event) { attachmentFile.value = (event.target as HTMLInputElement).files?.[0] ?? null; attachmentKey.value = crypto.randomUUID() }
const saveMutation = mutation(), reviseMutation = mutation(), deleteMutation = mutation()
type Category = { id: string; parent_id: string | null; name: string; version: number }
const categories = ref<Category[]>([])
const categoryMutation = mutation(), libraryEditMutation = mutation()
const selectedJob = ref(''), selectedSource = ref(''), loading = ref(false)
const categoryDialog = ref(false), editingCategory = ref<Category | null>(null)
const categoryForm = reactive({ name: '', parent_id: null as string | null })
const editingSource = ref<Detail | null>(null), sourceDialog = ref(false)
const sourceForm = reactive({ title: '', source_url: '', source_type: 'job_posting', category_id: '' })
const rootCategories = computed(() => categories.value.filter(item => !item.parent_id))
const currentRoot = computed(() => rootCategories.value.find(item => item.name === selectedJob.value))
const sourceNames = computed(() => {
  const root = rootCategories.value.find(item => item.name === jobCategory.value)
  return categories.value.filter(item => item.parent_id === root?.id).map(item => item.name)
})
function openCategory(item?: Category) {
  editingCategory.value = item ?? null
  categoryForm.name = item?.name ?? ''
  categoryForm.parent_id = item ? item.parent_id : currentRoot.value?.id ?? null
  categoryDialog.value = true
}
async function saveCategory() {
  if (!categoryForm.name.trim()) { ElMessage.warning('请输入分类名称'); return }
  busy.value = true
  try {
    const item = editingCategory.value
    const payload = { name: categoryForm.name.trim(), parent_id: categoryForm.parent_id, ...(item ? { expected_version: item.version } : {}) }
    const saved = await categoryMutation<Category>('/source-library/categories' + (item ? '/' + item.id : ''), 'POST', payload)
    if (!saved.parent_id) { selectedJob.value = saved.name; selectedSource.value = '' }
    else {
      selectedJob.value = rootCategories.value.find(root => root.id === saved.parent_id)?.name ?? ''
      selectedSource.value = saved.name
    }
    categoryDialog.value = false
    await load()
    ElMessage.success(item ? '分类已更新' : '分类已添加')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '保存失败') }
  finally { busy.value = false }
}
async function openSourceEdit(row: Row) {
  try {
    const item = await api<Detail>('/sources/' + row.source_id)
    editingSource.value = item
    Object.assign(sourceForm, { title: item.title ?? '', source_url: item.source_url ?? '', source_type: item.source_type, category_id: item.library_category_id ?? '' })
    sourceDialog.value = true
  } catch (error) { ElMessage.error(String(error)) }
}
async function saveSourceEdit() {
  if (!sourceForm.title.trim() || !sourceForm.category_id) { ElMessage.warning('请填写岗位名称并选择所属来源分类'); return }
  const item = editingSource.value
  if (!item) return
  busy.value = true
  try {
    await libraryEditMutation('/source-library/sources/' + item.source_id, 'POST', { ...sourceForm, title: sourceForm.title.trim(), source_url: sourceForm.source_url.trim() || null, expected_version: item.version })
    const leaf = categories.value.find(category => category.id === sourceForm.category_id)
    selectedJob.value = rootCategories.value.find(root => root.id === leaf?.parent_id)?.name ?? ''
    selectedSource.value = leaf?.name ?? ''
    sourceDialog.value = false
    await load()
    if (detail.value?.source_id === item.source_id) await view(item)
    ElMessage.success('岗位信息已保存')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '保存失败') }
  finally { busy.value = false }
}
const jobOptions = ['Agent 开发', '后端开发', '前端开发', '算法工程', '数据工程', '测试开发', '运维 / 平台工程', '产品 / 设计']
const sourceOptions = ['BOSS直聘', '猎聘', '智联招聘', '前程无忧', '牛客', '公司官网', '手动导入']
function jobGroup(row: Row): string {
  const leaf = categories.value.find(item => item.id === row.library_category_id)
  const root = categories.value.find(item => item.id === leaf?.parent_id)
  if (root) return root.name
  if (row.job_category?.trim()) return row.job_category.trim()
  const title = row.title ?? ''
  if (/agent|智能体|llm|大模型应用|rag/i.test(title)) return 'Agent 开发'
  if (/后端|backend|back.end|java|golang/i.test(title)) return '后端开发'
  if (/前端|frontend|front.end/i.test(title)) return '前端开发'
  if (/算法|机器学习|深度学习|algorithm/i.test(title)) return '算法工程'
  if (/数据|data/i.test(title)) return '数据工程'
  if (/测试|test|qa/i.test(title)) return '测试开发'
  if (/运维|devops|sre|平台工程/i.test(title)) return '运维 / 平台工程'
  if (/产品|设计|product|design/i.test(title)) return '产品 / 设计'
  return '未分类岗位'
}
function sourceGroup(row: Row): string {
  const leaf = categories.value.find(item => item.id === row.library_category_id)
  if (leaf) return leaf.name
  if (row.source_category?.trim()) return row.source_category.trim()
  if (!row.source_url) return '手动导入'
  try {
    const host = new URL(row.source_url).hostname.replace(/^www[.]/, '')
    const sites: Record<string, string> = { 'zhipin.com': 'BOSS直聘', 'liepin.com': '猎聘', 'zhaopin.com': '智联招聘', '51job.com': '前程无忧', 'nowcoder.com': '牛客', 'linkedin.com': 'LinkedIn', 'lagou.com': '拉勾' }
    return Object.entries(sites).find(([domain]) => host === domain || host.endsWith('.' + domain))?.[1] ?? host
  } catch { return '未分类来源' }
}
const jobGroups = computed(() => rootCategories.value.map(item => ({ ...item, count: rows.value.filter(row => jobGroup(row) === item.name).length })))
const jobRows = computed(() => rows.value.filter(row => jobGroup(row) === selectedJob.value))
const sourceGroups = computed(() => categories.value.filter(item => item.parent_id === currentRoot.value?.id).map(item => ({ ...item, count: jobRows.value.filter(row => sourceGroup(row) === item.name).length })))
const visibleRows = computed(() => jobRows.value.filter(row => sourceGroup(row) === selectedSource.value))
function goRoot() { selectedJob.value = ''; selectedSource.value = '' }
function chooseJob(name: string) { selectedJob.value = name; selectedSource.value = '' }
async function load() {
  loading.value = true
  try {
    categories.value = await api<Category[]>('/source-library/initialize', 'POST', {})
    const result: Row[] = []
    let cursor: string | null | undefined
    do {
      const params = new URLSearchParams({ limit: '100' })
      if (filter.value) params.set('source_type', filter.value)
      if (cursor) params.set('cursor', cursor)
      const page = await apiPage<Row>('/sources?' + params.toString())
      result.push(...page.data); cursor = page.next_cursor
    } while (cursor)
    rows.value = result
    if (!jobGroups.value.some(group => group.name === selectedJob.value)) goRoot()
    else if (!sourceGroups.value.some(group => group.name === selectedSource.value)) selectedSource.value = ''
  } catch (error) { ElMessage.error(String(error)) }
  finally { loading.value = false }
}
function openImport() { jobCategory.value = selectedJob.value; sourceCategory.value = selectedSource.value; showImport.value = true }
onMounted(load)
async function save() {
  busy.value = true
  try {
    let attachmentId: string | null = null
    if (form.import_mode === 'attachment') {
      if (!attachmentFile.value) throw new Error('请选择一个文本附件')
      attachmentId = (await uploadAttachment(attachmentFile.value, 'source', attachmentKey.value)).attachment_id
    }
    const payload: SourceImport = { ...form, job_category: jobCategory.value.trim() || null, source_category: sourceCategory.value.trim() || null, attachment_id: attachmentId, content: form.import_mode === 'text' ? form.content : null,
      source_url: form.source_url || null, title: form.title || null, published_at: null, published_at_precision: 'unknown' }
    if (form.source_type === 'job_posting' && form.import_mode === 'text') {
      await saveMutation('/job-data/import','POST',{job_category:jobCategory.value.trim()||null,source_category:sourceCategory.value.trim()||null,provider:provider.value,board_scope:boardScope.value||null,employer_name:employer.value||null,title:form.title||null,source_url:form.source_url||null,raw_payload:form.content})
    } else await saveMutation('/sources/import', 'POST', payload); showImport.value = false
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
      metadata_overrides: { source_type: correction.source_type, title: detail.value.title },
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
  <section class="page-heading"><div><span class="eyebrow">SOURCE LIBRARY</span><h1>岗位资料与来源</h1><p>按岗位分类、来源分类逐级查看岗位，保留原文与版本。</p></div><div class="library-actions"><el-button v-if="!selectedSource" type="primary" @click="openCategory()">{{ selectedJob ? '＋ 添加来源分类' : '＋ 添加岗位分类' }}</el-button><el-button :type="selectedSource ? 'primary' : 'default'" @click="openImport">{{ selectedSource ? '＋ 添加岗位' : '导入资料' }}</el-button></div></section>
  <CollectionPanel :job-category="selectedJob" @imported="load"/>
  <div class="library-toolbar"><el-select v-model="filter" placeholder="全部资料类型" clearable @change="goRoot(); load()"><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value" /></el-select><span>链接线索不计为已读正文</span></div>
  <nav class="library-path" aria-label="资料层级">
    <button type="button" :aria-current="!selectedJob ? 'page' : undefined" @click="goRoot">岗位分类</button>
    <template v-if="selectedJob"><span>/</span><button type="button" :aria-current="!selectedSource ? 'page' : undefined" @click="selectedSource = ''">{{ selectedJob }}</button></template>
    <template v-if="selectedSource"><span>/</span><strong>{{ selectedSource }}</strong></template>
  </nav>
  <section v-loading="loading" class="library-level">
    <div class="section-title library-level-heading"><h2>{{ !selectedJob ? '一级 · 岗位分类' : !selectedSource ? '二级 · 来源分类' : '三级 · 具体岗位与资料' }}</h2><span>{{ !selectedJob ? rows.length : !selectedSource ? jobRows.length : visibleRows.length }} 份资料</span></div>
    <div v-if="!selectedJob" class="library-groups">
      <article v-for="group in jobGroups" :key="group.id" class="library-group">
        <button type="button" class="library-group-main" @click="chooseJob(group.name)"><span class="eyebrow">岗位方向</span><h3>{{ group.name }}</h3><p>{{ group.count }} 份资料 · 查看来源 →</p></button>
        <el-button link type="primary" @click="openCategory(group)">编辑分类</el-button>
      </article>
    </div>
    <div v-else-if="!selectedSource" class="library-groups">
      <article v-for="group in sourceGroups" :key="group.id" class="library-group">
        <button type="button" class="library-group-main" @click="selectedSource = group.name"><span class="eyebrow">资料来源</span><h3>{{ group.name }}</h3><p>{{ group.count }} 份资料 · 查看岗位 →</p></button>
        <el-button link type="primary" @click="openCategory(group)">编辑分类</el-button>
      </article>
    </div>
    <el-empty v-if="!loading && (!selectedJob ? !jobGroups.length : !selectedSource ? !sourceGroups.length : false)" :description="selectedJob ? '该岗位分类下还没有来源分类，可点击上方按钮添加。' : '还没有岗位分类，可先添加分类，再添加来源和岗位。'" />
    <section v-if="selectedJob && selectedSource" class="table-card"><el-table :data="visibleRows" empty-text="还没有资料，先导入一份真实 JD。" @row-click="view"><el-table-column label="资料名称" min-width="260"><template #default="{ row }"><strong>{{ row.title || '未命名资料' }}</strong><small class="row-subtitle">{{ row.source_id.slice(0, 8) }}</small></template></el-table-column><el-table-column label="类型" width="140"><template #default="{ row }">{{ labels[row.source_type] }}</template></el-table-column><el-table-column label="正文状态" width="130"><template #default="{ row }"><el-tag :type="row.read_status === 'available' ? 'success' : 'warning'" effect="light">{{ row.read_status === 'available' ? '正文可读' : row.read_status === 'revoked' ? '正文已撤销' : '正文未读取' }}</el-tag></template></el-table-column><el-table-column prop="version" label="版本" width="80"/><el-table-column label="操作" width="210"><template #default="{ row }"><el-button link type="primary" @click.stop="view(row)">查看</el-button><el-button link type="primary" @click.stop="openSourceEdit(row)">编辑</el-button><el-button link type="danger" @click.stop="remove(row)">删除</el-button></template></el-table-column></el-table></section>
  </section>
  <el-dialog v-model="categoryDialog" :title="editingCategory ? '编辑分类' : categoryForm.parent_id ? '添加来源分类' : '添加岗位分类'" width="480px">
    <el-form label-position="top">
      <el-form-item label="分类名称"><el-input v-model="categoryForm.name" maxlength="100" show-word-limit @keyup.enter="!busy && saveCategory()"/></el-form-item>
      <el-form-item v-if="categoryForm.parent_id" label="所属岗位分类"><el-select v-model="categoryForm.parent_id"><el-option v-for="root in rootCategories" :key="root.id" :value="root.id" :label="root.name"/></el-select></el-form-item>
    </el-form>
    <template #footer><el-button @click="categoryDialog = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveCategory">保存分类</el-button></template>
  </el-dialog>
  <el-dialog v-model="sourceDialog" title="编辑岗位信息" width="580px">
    <el-form label-position="top">
      <el-form-item label="岗位 / 资料名称"><el-input v-model="sourceForm.title" maxlength="500"/></el-form-item>
      <el-form-item label="所属来源分类"><el-select v-model="sourceForm.category_id" filterable placeholder="选择岗位分类下的来源">
        <el-option-group v-for="root in rootCategories" :key="root.id" :label="root.name"><el-option v-for="leaf in categories.filter(item => item.parent_id === root.id)" :key="leaf.id" :value="leaf.id" :label="leaf.name"/></el-option-group>
      </el-select></el-form-item>
      <el-form-item label="来源链接"><el-input v-model="sourceForm.source_url" placeholder="https://…（可留空）"/></el-form-item>
      <el-form-item label="资料类型"><el-select v-model="sourceForm.source_type"><el-option v-for="(label, value) in labels" :key="value" :value="value" :label="label"/></el-select></el-form-item>
    </el-form>
    <p class="muted">正文在详情中通过“编辑正文”保存新版本，历史引用保留原文。</p>
    <template #footer><el-button @click="sourceDialog = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveSourceEdit">保存岗位</el-button></template>
  </el-dialog>
  <el-dialog v-model="showImport" title="添加岗位与来源资料" width="640px"><el-form label-position="top"><el-form-item label="岗位分类"><el-select v-model="jobCategory" filterable allow-create default-first-option clearable placeholder="选择或输入岗位方向"><el-option v-for="name in [...new Set([...jobOptions, ...jobGroups.map(group => group.name)])]" :key="name" :label="name" :value="name"/></el-select></el-form-item><el-form-item label="来源分类"><el-select v-model="sourceCategory" filterable allow-create default-first-option clearable placeholder="留空时按来源链接归类"><el-option v-for="name in [...new Set([...sourceOptions, ...sourceNames])]" :key="name" :label="name" :value="name"/></el-select></el-form-item><el-form-item label="资料类型"><el-select v-model="form.source_type"><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value"/></el-select></el-form-item><template v-if="form.source_type==='job_posting' && form.import_mode==='text'"><el-form-item label="原文格式（仅导入保存的数据，不联网）"><el-select v-model="provider"><el-option v-for="v in ['manual_import','greenhouse','lever','ashby','json_ld']" :key="v" :value="v" :label="v"/></el-select></el-form-item><el-form-item label="招聘板 / 来源范围（有外部 ID 时填写）"><el-input v-model="boardScope"/></el-form-item><el-form-item label="公司（来源未提供时填写）"><el-input v-model="employer"/></el-form-item></template><el-form-item label="标题（可留空）"><el-input v-model="form.title"/></el-form-item><el-form-item label="原始链接（粘贴正文时可留空）"><el-input v-model="form.source_url" placeholder="https://…"/></el-form-item><el-form-item label="导入方式"><el-radio-group v-model="form.import_mode"><el-radio value="text">粘贴正文</el-radio><el-radio value="attachment">文本附件</el-radio><el-radio value="link_clue">只保存链接线索</el-radio></el-radio-group></el-form-item><el-form-item v-if="form.import_mode === 'text'" label="原文（最多 100,000 字符）"><el-input v-model="form.content" type="textarea" :rows="10" :maxlength="100000" show-word-limit/></el-form-item><el-form-item v-else-if="form.import_mode === 'attachment'" label="UTF-8 文本附件（最多 1 MiB）"><input type="file" accept=".txt,.md,.json,.log,.py,.xml" @change="selectAttachment" /></el-form-item><el-alert v-else type="info" :closable="false" title="仅保存链接，不自动读取，也不纳入 JD 统计。支持指定岗位和来源分类。"/></el-form><template #footer><el-button @click="showImport = false">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存资料</el-button></template></el-dialog>
  <el-drawer :model-value="!!detail" title="来源与版本" size="min(720px, 100vw)" @close="detail = null"><template v-if="detail"><h2>{{ detail.title || '未命名资料' }}</h2><p class="muted">{{ detail.source_url || '手动导入 · 未提供原链接' }}</p><el-select v-model="selectedSnapshot" placeholder="尚无正文快照" @change="readSnapshot"><el-option v-for="item in detail.snapshots" :key="item.source_snapshot_id" :label="`快照 v${item.version_no} · ${item.source_snapshot_id.slice(0, 8)}`" :value="item.source_snapshot_id"/></el-select><el-button @click="openSourceEdit(detail)">编辑岗位信息</el-button><el-button @click="correct">{{ snapshot ? '编辑正文（保留历史）' : '补充正文' }}</el-button><template v-if="snapshot"><div class="snapshot-meta"><el-tag>{{ labels[String(snapshot.metadata.source_type)] }}</el-tag><span>发布时间：{{ snapshot.metadata.published_at || '未知' }}</span></div><pre class="source-body">{{ snapshot.content }}</pre><small class="hash">SHA-256 {{ snapshot.content_hash }}</small></template><el-empty v-else description="链接已保存，正文未读取。可以补贴正文后继续。"/><JobContractPanel v-if="detail.source_type==='job_posting' && selectedSnapshot" :key="selectedSnapshot" :snapshot-id="selectedSnapshot"/></template></el-drawer>
  <el-dialog v-model="correcting" title="保存新的来源快照" width="620px"><el-form label-position="top"><el-form-item label="资料类型"><el-select v-model="correction.source_type"><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value"/></el-select></el-form-item><el-form-item label="正文"><el-input v-model="correction.content" type="textarea" :rows="10" :maxlength="100000"/></el-form-item><el-form-item label="更正原因"><el-input v-model="correction.reason"/></el-form-item></el-form><template #footer><el-button type="primary" :loading="busy" @click="revise">保存新快照</el-button></template></el-dialog>
</template>

<style scoped>
.library-path{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:0 0 22px;font-size:14px}
.library-path button{border:0;background:none;padding:5px 0;color:#247968;cursor:pointer}
.library-path button[aria-current="page"]{color:#253343;font-weight:600}
.library-path span{color:#a2adac}
.library-level-heading{margin:0 0 18px}
.library-groups{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:18px}
.library-actions{display:flex;flex-wrap:wrap;gap:10px}.library-actions .el-button{margin-left:0}
.library-group-main{display:block;width:100%;text-align:left;border:0;background:none;padding:0;cursor:pointer;color:inherit;margin-bottom:14px}
.library-group{text-align:left;padding:24px;border:1px solid #e2e8eb;border-radius:12px;background:white;cursor:pointer;color:inherit;transition:border-color .15s,box-shadow .15s}
.library-group:hover,.library-group:focus-visible{border-color:#247968;box-shadow:0 5px 18px #24796812}
.library-group h3{font-size:19px;margin:18px 0 12px;overflow-wrap:anywhere}
.library-group p{margin:0;font-size:13px;color:#7a878b}
</style>
