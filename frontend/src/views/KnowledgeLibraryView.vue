<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, apiPage, mutation } from '../api/client'
import KnowledgeSearch from '../components/KnowledgeSearch.vue'

type Project = { repository_snapshot_id: string; name: string; description?: string; tags?: string[]; github_url: string; commit: string; indexed: boolean; revision: number; file_count?: number; chunk_count?: number; languages?: string[]; commit_verified: boolean; truncated: boolean; license_status: string; coverage?: unknown }
type Plan = { plan_id: string; version: number; objectives: { objective_version_id: string; title: string }[] }
const route = useRoute(), router = useRouter(), write = mutation()
const projects = ref<Project[]>([]), snapshotId = ref(''), plans = ref<Plan[]>([]), objectiveId = ref(String(route.query.objective ?? ''))
const busy = ref(false), importDialog = ref(false), metadataDialog = ref(false), importMode = ref('github'), files = ref<Record<string,string>>({}), skipped = ref(0)
const form = reactive({ name: '', github_url: '', requested_ref: '', description: '', tags: '' })
const metadata = reactive({ name: '', description: '', tags: '', expected_revision: 0 })
const current = computed(() => projects.value.find(p => p.repository_snapshot_id === snapshotId.value))
const objectives = computed(() => [...new Map(plans.value.flatMap(p => p.objectives.map(o => [o.objective_version_id, o] as const))).values()])
const title = computed(() => objectives.value.find(o => o.objective_version_id === objectiveId.value)?.title ?? '')
async function load() {
  try {
    projects.value = await api<Project[]>('/knowledge/projects')
    let cursor: string|null = null; const values: Plan[] = []
    do { const page: {data: Plan[]; next_cursor?: string|null} = await apiPage<Plan>('/plans?limit=100' + (cursor ? '&cursor=' + cursor : '')); values.push(...page.data); cursor = page.next_cursor ?? null } while(cursor)
    plans.value = values
  } catch (e) { ElMessage.error(String(e)) }
}
async function importFolder(event: Event) {
  const input = event.target as HTMLInputElement; const selected: Record<string,string> = {}; skipped.value = 0
  try {
    let size = 0
    for (const file of Array.from(input.files ?? [])) {
      const name = file.webkitRelativePath.split('/').slice(1).join('/') || file.name
      if(name.split('/').some(p => ['.git','.env','.aws','.ssh','.codex','.venv','venv','node_modules','__pycache__','dist','build','vendor','.idea'].includes(p.toLowerCase()) || p.toLowerCase().startsWith('.env.')) || file.size > 100000 || !(/\.(py|js|ts|tsx|jsx|vue|java|go|rs|c|cpp|h|cs|md|rst|txt|toml|yaml|yml|json|sql|html|css|xml)$/i.test(name) || /(^|\/)(license|licence|notice|dockerfile|makefile)$/i.test(name))) { skipped.value++; continue }
      const text = await file.text()
      if(!text.trim() || text.includes('\u0000')) { skipped.value++; continue }
      size += new TextEncoder().encode(text).length
      if(size > 12000000 || Object.keys(selected).length >= 1000) throw new Error('请选择更小的源码目录，最多 1000 个文件 / 12 MB')
      selected[name] = text
    }
    files.value = selected
  } catch(e) { files.value = {}; ElMessage.error(String(e)) } finally { input.value = '' }
}
async function submitImport() {
  if(importMode.value === 'directory' && !Object.keys(files.value).length) { ElMessage.warning('请先选择源码目录'); return }
  busy.value = true
  try {
    const result = await write<Project>('/knowledge/imports', 'POST', { ...form, tags: form.tags.split(/[,，]/).map(s => s.trim()).filter(Boolean), files: importMode.value === 'directory' ? files.value : null })
    snapshotId.value = result.repository_snapshot_id; importDialog.value = false; await load(); ElMessage.success('已导入并建立代码索引')
  } catch(e) { ElMessage.error(String(e)) } finally { busy.value = false }
}
async function index(project: Project) {
  busy.value = true
  try { await write('/knowledge/snapshots/' + project.repository_snapshot_id + '/index', 'POST', {}); await load(); snapshotId.value = project.repository_snapshot_id; ElMessage.success('索引已就绪') }
  catch(e) { ElMessage.error(String(e)) } finally { busy.value = false }
}
function editMetadata() {
  if(!current.value) return
  Object.assign(metadata, { name: current.value.name, description: current.value.description ?? '', tags: current.value.tags?.join('，') ?? '', expected_revision: current.value.revision }); metadataDialog.value = true
}
async function saveMetadata() {
  if(!current.value) return
  busy.value = true
  try { await write('/knowledge/snapshots/' + snapshotId.value + '/metadata', 'PUT', { ...metadata, tags: metadata.tags.split(/[,，]/).map(s => s.trim()).filter(Boolean) }); metadataDialog.value = false; await load() }
  catch(e) { ElMessage.error(String(e)) } finally { busy.value = false }
}
function open(citationId: string) { void router.push({ path: '/studio', query: { citation: citationId, ...(objectiveId.value ? {objective: objectiveId.value} : {}) } }) }
onMounted(load)
</script>
<template>
  <section class="page-heading"><div><span class="eyebrow">开源实现 · 固定版本 · 可追溯定位</span><h1>代码学习库</h1><p>检索学习主题，找到具体实现，再关联到自己的学习计划。</p></div><div><el-button @click="load">刷新</el-button><el-button type="primary" @click="importDialog=true">导入开源项目</el-button></div></section>
  <div class="library-layout">
    <aside class="library-card"><h2>项目版本</h2><el-button link @click="snapshotId=''">检索全部已索引项目</el-button><el-empty v-if="!projects.length" description="导入第一个参考项目"/>
      <article v-for="project in projects" :key="project.repository_snapshot_id" class="project" :class="{ active: snapshotId === project.repository_snapshot_id }"><button @click="snapshotId=project.repository_snapshot_id"><strong>{{ project.name }}</strong><small>{{ project.commit.slice(0,12) }}</small><span>{{ project.indexed ? `${project.file_count} 文件 · ${project.chunk_count} 片段` : '尚未索引' }}</span></button><el-tag size="small" :type="project.commit_verified ? 'success' : 'info'">{{ project.commit_verified ? 'GitHub 固定版本' : '预存 / 用户声明版本' }}</el-tag><el-button v-if="!project.indexed" link :loading="busy" @click="index(project)">建立索引</el-button></article>
    </aside>
    <section class="library-card content"><template v-if="current"><div class="project-heading"><h2>{{ current.name }}</h2><el-button v-if="current.indexed" @click="editMetadata">编辑学习主题</el-button></div><a :href="current.github_url+'/tree/'+current.commit" target="_blank" rel="noopener noreferrer">查看原始版本 ↗</a><p>{{ current.description || '可补充项目用途、适合学习的模块与主题标签。' }}</p><el-tag v-for="tag in current.tags" :key="tag" effect="plain">{{tag}}</el-tag><p class="muted">{{current.languages?.join(' / ')}} · 许可证识别：{{current.license_status}} · {{current.truncated ? '部分文本快照，存在未导入内容' : '已导入筛选后的源码与文档'}} · 从未执行导入代码</p><details><summary>导入覆盖范围</summary><pre>{{JSON.stringify(current.coverage,null,2)}}</pre></details></template><h2 v-else>跨项目代码检索</h2>
      <div class="goal-selector"><span>关联学习目标（可选）</span><el-select v-model="objectiveId" clearable filterable placeholder="选择已有学习目标"><el-option v-for="o in objectives" :key="o.objective_version_id" :value="o.objective_version_id" :label="o.title"/></el-select></div>
      <KnowledgeSearch :snapshot-id="snapshotId" :objective-id="objectiveId" :objective-title="title" @open="open"/>
    </section>
  </div>
  <el-dialog v-model="importDialog" title="导入开源项目的固定版本" width="min(700px,95vw)" :close-on-click-modal="!busy">
    <el-radio-group v-model="importMode"><el-radio-button value="github">GitHub 公开仓库</el-radio-button><el-radio-button value="directory">本地源码目录副本</el-radio-button></el-radio-group>
    <el-form label-position="top"><el-form-item label="项目名称"><el-input v-model="form.name" :maxlength="100"/></el-form-item><el-form-item label="GitHub 仓库地址"><el-input v-model="form.github_url" placeholder="https://github.com/owner/repository"/></el-form-item><el-form-item label="完整 commit SHA（40 位）"><el-input v-model="form.requested_ref" placeholder="固定版本，不使用 main 或 latest" :maxlength="40"/></el-form-item><el-form-item label="适合学习什么"><el-input v-model="form.description" type="textarea" :maxlength="2000"/></el-form-item><el-form-item label="主题标签，以逗号分隔"><el-input v-model="form.tags" placeholder="工具调用，状态恢复，异步"/></el-form-item></el-form>
    <template v-if="importMode==='directory'"><input type="file" webkitdirectory multiple aria-label="选择知识库源码目录" @change="importFolder"/><p>已选择 {{Object.keys(files).length}} 个文本文件，跳过 {{skipped}} 个文件。</p><el-alert title="目录只读复制。commit 由你声明，系统不会将其标为远端已验证；请核对待导入内容。" type="info" :closable="false"/></template>
    <p class="muted">不运行仓库脚本。跳过私密与依赖目录、二进制和过大文件；单文件 100 KB，总计 1000 文件 / 12 MB。GitHub 下载最多 20 MB，覆盖范围会保存。</p>
    <template #footer><el-button :disabled="busy" @click="importDialog=false">取消</el-button><el-button type="primary" :loading="busy" @click="submitImport">导入并建立索引</el-button></template>
  </el-dialog>
  <el-dialog v-model="metadataDialog" title="编辑项目学习主题" width="min(600px,95vw)"><el-form label-position="top"><el-form-item label="名称"><el-input v-model="metadata.name"/></el-form-item><el-form-item label="用途与学习说明"><el-input v-model="metadata.description" type="textarea" :maxlength="2000"/></el-form-item><el-form-item label="主题标签"><el-input v-model="metadata.tags"/></el-form-item></el-form><template #footer><el-button :loading="busy" type="primary" @click="saveMetadata">保存并更新检索资料</el-button></template></el-dialog>
</template>
<style scoped>
.library-layout{display:grid;grid-template-columns:245px minmax(0,1fr);gap:18px}.library-card{background:white;border:1px solid #dce5e0;border-radius:12px;padding:18px;min-width:0}.library-card h2{font-size:17px;margin:0 0 14px}.project{padding:12px 8px;border-bottom:1px solid #e4eae6;border-radius:8px}.project.active{background:#edf5ef}.project>button{border:0;background:transparent;display:block;width:100%;text-align:left;cursor:pointer;color:#294a45;overflow-wrap:anywhere}.project strong,.project small,.project span{display:block;line-height:1.9}.project small,.project span{font-size:12px;color:#74847d}.project-heading{display:flex;justify-content:space-between;align-items:center}.content>p{font-size:13px;line-height:1.8}.content>.el-tag{margin:4px}.content a{font-size:12px;color:#38644f;overflow-wrap:anywhere}.muted{font-size:12px;color:#74847d;line-height:1.8}.goal-selector{display:flex;align-items:center;gap:12px;padding:18px 0}.goal-selector span{font-size:13px;white-space:nowrap}.goal-selector .el-select{flex:1}details{font-size:12px;color:#74847d}details pre{white-space:pre-wrap;max-height:200px;overflow:auto}@media(max-width:900px){.library-layout{grid-template-columns:1fr}.library-layout>aside{max-height:300px;overflow:auto}}
</style>
