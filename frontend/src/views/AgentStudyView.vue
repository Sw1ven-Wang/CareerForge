<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiPage, mutation } from '../api/client'
import ReviewInvestigation from '../components/ReviewInvestigation.vue'
import { type StudyTrack, intentNames, roleNames, activeStates } from '../types/study'
const router=useRouter(),write=mutation()
const tracks=ref<{track_id:string;name:string;available:boolean}[]>([]),id=ref(''),track=ref<StudyTrack|null>(null)
const sources=ref<{source_id:string;title:string;read_status:string}[]>([]),sourceIds=ref<string[]>([]),name=ref('Agent 开发'),target=ref(5)
const policy=ref<'coverage'|'strict_count'>('strict_count'),newPolicy=ref<'coverage'|'strict_count'>('coverage')
const batchCandidates=ref<string[]>([])
const createOpen=ref(false),busy=ref(false),showReview=ref(''),tokenCap=ref(8192),reviewCalls=ref(1)
const intent=ref('learn_project'),focus=ref(''),reqKeys=ref<string[]>([]),reviewIds=ref<string[]>([])
const focusChunk=ref(''),focusHits=ref<{chunk_id:string;symbol:string;path:string;start_line:number}[]>([]),searchBusy=ref(false)
const existing=ref<{repository_snapshot_id:string;name:string}[]>([]),existingId=ref('')
const running=computed(()=>!!track.value?.active_run&&activeStates.includes(track.value.active_run.status))
const approved=computed(()=>track.value?.approved_projects??[])
const countMet=computed(()=>!!track.value&&track.value.approved_count>=track.value.target_count)
const selectedCoverage=computed(()=>{
 const located=new Set(approved.value.filter(p=>reviewIds.value.includes(p.id)).flatMap(p=>p.highlights.map(h=>h.requirement_key)))
 return {covered:reqKeys.value.filter(k=>located.has(k)),missing:reqKeys.value.filter(k=>!located.has(k))}
})
const ready=computed(()=>!!track.value&&reqKeys.value.length>0&&reviewIds.value.length>0&&!selectedCoverage.value.missing.length&&(track.value.collection_policy==='coverage'||countMet.value))
const researchKeys=computed(()=>reqKeys.value.length?reqKeys.value:track.value?.coverage.missing_keys??[])
const canResearch=computed(()=>!!track.value?.requirements&&(researchKeys.value.length>0||track.value.collection_policy==='strict_count'))
const reviewed=computed(()=>new Map(track.value?.reviews.map(r=>[r.candidate_id,r])??[]))
const pending=computed(()=>track.value?.candidates.filter(c=>!reviewed.value.has(c.id))??[])
const displayedRequirements=computed(()=>track.value?.requirements??track.value?.extraction_preview)
const latestPlan=computed(()=>track.value?.plans.at(-1))
let timer:ReturnType<typeof setTimeout>|undefined,alive=true,loadNo=0
async function refresh(){
 clearTimeout(timer)
 if(!id.value)return
 const serial=++loadNo
 try{const result=await api<StudyTrack>('/study/tracks/'+id.value);if(serial!==loadNo)return;track.value=result;target.value=result.target_count;policy.value=result.collection_policy
 reqKeys.value=reqKeys.value.filter(k=>result.requirements?.requirements.some(r=>r.key===k))
 reviewIds.value=reviewIds.value.filter(k=>result.approved_projects.some(r=>r.id===k))
 }catch(e){ElMessage.error(String(e));track.value=null}
 if(alive&&running.value)timer=setTimeout(refresh,2500)
}
async function reviewBatch(){
 if(!track.value||!batchCandidates.value.length)return
 const total=batchCandidates.value.length*reviewCalls.value
 try{await ElMessageBox.confirm(`为 ${batchCandidates.value.length} 个已留存固定版本分别建立审核子任务，父任务预留最多 ${total} 次模型调用，每次最多 ${tokenCap.value} 输出 token。命令不会执行，金额按供应商计费。`,'启动审核批次',{confirmButtonText:'预留额度并启动',cancelButtonText:'取消'});busy.value=true
 await write('/study/tracks/'+id.value+'/review-batches','POST',{expected_revision:track.value.revision,candidate_ids:batchCandidates.value,per_child_call_limit:reviewCalls.value,call_budget:total,max_output_tokens:tokenCap.value});await refresh()
 }catch(e){if(e!=='cancel'&&e!=='close')ElMessage.error(String(e))}finally{busy.value=false}
}
async function load(){
 try{tracks.value=await api('/study/tracks');sources.value=(await apiPage<{source_id:string;title:string;read_status:string}>('/sources')).data.filter(s=>s.read_status==='available');existing.value=await api('/knowledge/projects')
 if(!id.value)id.value=tracks.value.find(t=>t.available)?.track_id??''
 else await refresh()
 }catch(e){ElMessage.error(String(e))}
}
watch(id,()=>{clearTimeout(timer);track.value=null;reqKeys.value=[];reviewIds.value=[];void refresh()})
onMounted(load);onBeforeUnmount(()=>{alive=false;loadNo++;clearTimeout(timer)})
async function create(){
 busy.value=true
 try{const versions=await Promise.all(sourceIds.value.map(async sid=>(await api<{snapshots:{source_snapshot_id:string}[]}>('/sources/'+sid)).snapshots[0]?.source_snapshot_id))
 if(versions.some(v=>!v))throw new Error('请选择有正文的资料')
 const result=await write<StudyTrack>('/study/tracks','POST',{name:name.value,source_snapshot_ids:versions,target_count:target.value,collection_policy:newPolicy.value})
 createOpen.value=false;await load();id.value=result.track_id;await refresh()
 }catch(e){ElMessage.error(String(e))}finally{busy.value=false}
}
async function run(role:string,candidateId?:string){
 if(!track.value)return
 try{
 const callLimit=role==='discover'?1+5*reviewCalls.value:role==='review'?reviewCalls.value:1
 const gap=role==='plan'&&track.value.collection_policy==='coverage'&&!countMet.value
 const scopeNote=role==='plan'?`所选目标的位置覆盖 ${selectedCoverage.value.covered.length}/${reqKeys.value.length}；解释的语义支持未独立核验。${gap?`收集偏好尚差 ${track.value.target_count-track.value.approved_count} 个项目；确认只为当前选定范围制定计划。`:''}`:''
 await ElMessageBox.confirm(`${scopeNote} ${roleNames[role]}：最多 ${callLimit} 次模型调用，输出上限 ${tokenCap.value} token。${role==='discover'?'另执行最多 3 次 GitHub 公开仓库搜索并审核最多 5 个候选，达到目标即停止；每候选预算包含最终审核报告。':role==='review'?'调查固定版本的符号、相关实现与测试；最多12次只读工具/6次源码读取，不执行项目。':''} 供应商按实际用量计费。`,'启动 Agent',{confirmButtonText:'启动本轮',cancelButtonText:'取消'})
 busy.value=true
 const body:Record<string,unknown>={expected_revision:track.value.revision,role,max_output_tokens:tokenCap.value,model_call_limit:1}
 if(role==='review'||role==='discover')body.review_call_limit=reviewCalls.value
 if(candidateId)body.candidate_id=candidateId
 if(role==='discover'){body.auto_review_count=5;if(researchKeys.value.length)body.research_requirement_keys=researchKeys.value}
 if(role==='plan'){body.acknowledge_collection_gap=gap;body.intent={kind:intent.value,focus:focus.value,focus_chunk_id:focusChunk.value||null,requirement_keys:reqKeys.value,project_review_ids:reviewIds.value}}
 await write('/study/tracks/'+id.value+'/runs','POST',body);await refresh()
 }catch(e){if(e!=='cancel'&&e!=='close')ElMessage.error(String(e))}finally{busy.value=false}
}
async function settings(){
 if(!track.value)return
 try{
 if(track.value.collection_policy==='strict_count'&&policy.value==='coverage')await ElMessageBox.confirm('此方向将改为覆盖优先，数量作为收集偏好。已有明确数量要求只有在你确认后才改变。','修改数量策略')
 track.value=await api('/study/tracks/'+id.value,'PUT',{expected_revision:track.value.revision,target_count:target.value,collection_policy:policy.value})
 }catch(e){ElMessage.error(String(e))}
}
async function findFocus(){
 if(!focus.value.trim()||!reviewIds.value.length)return
 searchBusy.value=true;focusChunk.value='';focusHits.value=[]
 try{const results=await Promise.all(approved.value.filter(p=>reviewIds.value.includes(p.id)).map(p=>api<{results:{chunk_id:string;symbol:string;path:string;start_line:number}[]}>('/knowledge/search?q='+encodeURIComponent(focus.value)+'&snapshot_id='+p.repository_snapshot_id+'&limit=5')))
 focusHits.value=results.flatMap(r=>r.results).filter(h=>/\.(py|js|ts|tsx|jsx|vue|java|go|rs|c|cpp|h|cs|sql)$/i.test(h.path)).slice(0,20)
 }catch(e){ElMessage.error(String(e))}finally{searchBusy.value=false}
}
watch([intent,reviewIds],()=>{focusChunk.value='';focusHits.value=[]},{deep:true})
async function addCandidate(){
 if(!track.value||!existingId.value)return
 try{track.value=await write('/study/tracks/'+id.value+'/candidates','POST',{expected_revision:track.value.revision,repository_snapshot_id:existingId.value});existingId.value=''}catch(e){ElMessage.error(String(e))}
}
async function cancel(){
 if(!track.value?.active_run)return
 try{await api('/runs/'+track.value.active_run.run_id+'/cancel','POST',{expected_version:track.value.active_run.version});await refresh()}catch(e){ElMessage.error(String(e))}
}
function learn(){if(latestPlan.value)void router.push({path:'/studio',query:{track:id.value,study_plan:latestPlan.value.id}})}
</script>
<template>
<section class="page-heading"><div><span class="eyebrow">CAREER → CODE → LEARNING</span><h1>Agent 学习工作台</h1><p>你提供职业需求，Agent 找到值得学的实现，再陪你把它学会。</p></div><el-button type="primary" @click="createOpen=true">新建学习方向</el-button></section>
<div class="track-bar"><el-select v-model="id" placeholder="选择学习方向"><el-option v-for="t in tracks" :key="t.track_id" :value="t.track_id" :label="t.name" :disabled="!t.available"/></el-select><el-button @click="load">刷新</el-button><router-link to="/studio">打开 IDE</router-link></div>
<el-empty v-if="!track" description="先导入职位正文，再创建一个学习方向。"><router-link to="/sources">导入需求资料 →</router-link></el-empty>
<template v-if="track">
<div class="flow-strip"><span :class="{done:track.sources.length}">人工需求</span><b>→</b><span :class="{done:track.requirements}">技术要求</span><b>→</b><span :class="{done:track.candidates.length}">研究与审核</span><b>→</b><span :class="{done:track.approved_count>0}">项目库 {{track.approved_count}} · {{track.collection_policy==='strict_count'?'必须达到':'收集偏好'}} {{track.target_count}}</span><b>→</b><span :class="{done:latestPlan}">模块化学习</span></div>
<div v-if="track.active_run" class="run-status"><span>{{roleNames[track.active_run.role]}} · {{track.active_run.status}}</span><el-button v-if="running" link @click="cancel">停止本轮</el-button><router-link to="/runs">查看运行记录</router-link></div>
<el-alert v-if="track.active_run?.status==='failed'&&!track.extraction_preview&&!track.requirements?.recovery" title="本轮未发布结果。请在运行记录查看原因；系统没有自动追加收费重试。" type="warning" :closable="false"/>
<el-alert v-if="track.extraction_preview?.budget_warning" :title="`供应商报告输出 ${track.extraction_preview.budget_warning.reported} token，超过请求上限 ${track.extraction_preview.budget_warning.requested}。本轮已停止，以下仅为未发布预览，调研与审核未启动。`" type="warning" :closable="false"/>
<el-alert v-if="track.requirements?.recovery" :title="`原提取运行校验失败；已从保存结果恢复 ${track.requirements.requirements.length} 条有效需求，未追加模型调用。待核对 ${track.requirements.unresolved?.length??0} 条。`" type="info" :closable="false"/>
<div class="study-columns">
<section class="surface study-card"><header><h2>① 技术需求</h2><el-button :disabled="running" :loading="busy" @click="run('extract')">{{track.requirements?'重新提取':'提取需求'}}</el-button></header><p class="muted">了解 / 熟悉 / 掌握是目标深度。推断会单独标注。</p><p class="source-chips"><span v-for="s in track.sources" :key="s.source_snapshot_id">{{s.title}}</span></p>
<el-empty v-if="!displayedRequirements" description="尚未提取技术要求"/><p v-if="track.extraction_preview" class="muted">已校验 {{track.extraction_preview.requirements.length}} 条原文依据；{{track.extraction_preview.unresolved.length}} 条待核对。</p><el-tag v-if="track.extraction_preview" type="warning">未发布预览 · 不进入后续链路</el-tag>
<el-tag v-if="track.requirements?.synthetic" type="warning">离线模拟</el-tag>
<article v-for="r in displayedRequirements?.requirements" :key="r.key" class="requirement"><div><strong>{{r.technology}}</strong><el-tag :type="r.depth==='掌握'?'success':'info'" size="small">{{r.depth}}</el-tag><small>{{r.depth_basis==='explicit'?'原文明确':'模型推断'}}</small></div><p>{{r.rationale}}</p><details><summary>原文依据 · {{r.evidence.length}}</summary><blockquote v-for="(e,i) in r.evidence" :key="i">{{e.quote}}<small>{{e.source_title}}</small></blockquote><small>检索词：{{r.keywords.join('、')}}</small></details></article>
<details v-if="track.requirements?.unresolved?.length"><summary>未纳入后续链路的待核对条目 · {{track.requirements.unresolved.length}}</summary><p v-for="item in track.requirements.unresolved" :key="item.key">{{item.technology}}：{{item.reason}}</p></details>
<details v-if="track.extraction_preview?.unresolved.length"><summary>待核对条目</summary><p v-for="item in track.extraction_preview.unresolved" :key="item.key">{{item.technology}}：{{item.reason}}</p></details>
<details v-if="track.extraction_preview?.normalization_repairs.length"><summary>本地等级校正 · {{track.extraction_preview.normalization_repairs.length}} 项</summary><p v-for="item in track.extraction_preview.normalization_repairs" :key="item.key">{{item.key}}：{{item.original_depth}} / {{item.original_basis}} → {{item.depth}} / {{item.depth_basis}}。{{item.reason}}</p></details>
</section>
<section class="surface study-card"><header><h2>② 开源项目研究与审核</h2><el-button type="primary" plain :disabled="!canResearch||running" :loading="busy" @click="run('discover')">调研并审核一批</el-button></header><p class="muted">先发现候选，再读取固定版本代码审核。强相关且质量条件满足才入库。</p>
<div class="target-setting"><span>{{policy==='strict_count'?'严格数量要求':'收集偏好'}}</span><el-input-number v-model="target" :min="1" :max="10" size="small"/><el-select v-model="policy" aria-label="数量策略" style="width:200px"><el-option label="覆盖优先，数量为偏好" value="coverage"/><el-option label="必须达到指定数量" value="strict_count"/></el-select><el-button size="small" :disabled="running||(target===track.target_count&&policy===track.collection_policy)" @click="settings">保存策略</el-button><strong>{{track.approved_count}} 个已入库</strong></div>
<p class="muted">新审核会记录符号调查与补读轨迹，历史审核不自动重跑。已有方向沿用原严格数量要求，只有你保存策略才改变。调研默认补缺失位置的技术，也可在下方选择要补充的技术。</p>
<details open class="coverage"><summary>技术目标覆盖 · 已定位 {{track.coverage.located}} / {{track.coverage.total}}</summary><div class="table-scroll"><table><thead><tr><th>技术目标</th><th>代码位置</th><th>语义支持</th><th>运行观察</th></tr></thead><tbody><tr v-for="r in track.coverage.rows" :key="r.requirement_key"><td>{{r.technology}} · {{r.depth}}</td><td>{{r.located_project_ids.length?`${r.located_project_ids.length} 个审核项目`:'缺少依据'}}</td><td>{{r.located_project_ids.length?'未独立核验':'无依据'}}</td><td>无运行观察</td></tr></tbody></table></div><p class="muted">{{track.coverage.scope}}</p></details>
<el-empty v-if="!track.candidates.length" description="等待技术关键词调研，或送审已有项目"/>
<article v-for="c in track.candidates" :key="c.id" class="project"><div class="project-title"><a :href="c.github_url" target="_blank" rel="noopener noreferrer">{{c.full_name}}</a><el-tag size="small" :type="reviewed.get(c.id)?.admitted?'success':'info'">{{reviewed.get(c.id)?.admitted?'已入库':reviewed.get(c.id)?({strong:'相关但未入库',weak:'弱相关',uncertain:'待补证据'}[reviewed.get(c.id)!.verdict as 'strong'|'weak'|'uncertain']):'候选待审'}}</el-tag></div><p>{{c.description}}</p><small>★ {{c.stars??'未记录'}} · {{c.license??'许可证待核对'}} · {{c.origin==='user_import'?'人工项目':'GitHub 检索'}}</small><div><el-button size="small" :disabled="running" @click="run('review',c.id)">{{reviewed.has(c.id)?'重新审核':'审核此项目'}}</el-button><el-button v-if="reviewed.has(c.id)" link @click="showReview=showReview===c.id?'':c.id">审核依据</el-button></div>
<div v-if="showReview===c.id&&reviewed.get(c.id)" class="audit"><p>{{reviewed.get(c.id)?.summary}}</p><el-tag size="small">{{reviewed.get(c.id)?.evidence?.length||reviewed.get(c.id)?.highlights.length?'代码位置已校验':'尚无有效源码引用'}}</el-tag> <el-tag size="small" type="warning">语义支持未独立核验</el-tag><p class="muted">相关性来自模型判断；静态摘录校验不能证明解释正确或代码实际运行。</p><small>技术栈：{{reviewed.get(c.id)?.technology_stack.join('、')}}</small><ul><li v-for="q in reviewed.get(c.id)?.quality_notes" :key="q">{{q}}</li></ul><el-button v-for="h in reviewed.get(c.id)?.highlights" :key="h.citation_id" link @click="router.push({path:'/studio',query:{citation:h.citation_id}})">{{h.requirement_key}} → {{h.path}}:{{h.start_line}}</el-button><ReviewInvestigation :review="reviewed.get(c.id)!"/><p v-for="l in reviewed.get(c.id)?.limitations" :key="l" class="muted">{{l}}</p></div></article>
<details><summary>用子任务审核固定版本（最多两个）</summary><p class="muted">独立上下文、统一预留与结算；取消父任务会停止全部子任务。候选需已有导入或审核留存的固定版本，结果由父任务统一校验后入库。</p><el-select v-model="batchCandidates" multiple :multiple-limit="2" placeholder="选择要审核的候选"><el-option v-for="c in track.candidates" :key="c.id" :value="c.id" :label="c.full_name"/></el-select><el-button :disabled="running||!batchCandidates.length" :loading="busy" @click="reviewBatch">预留额度并启动审核批次</el-button></details><details><summary>送审已导入的开源项目</summary><div class="existing"><el-select v-model="existingId" placeholder="选择已有固定版本"><el-option v-for="p in existing" :key="p.repository_snapshot_id" :value="p.repository_snapshot_id" :label="p.name"/></el-select><el-button :disabled="!existingId||!track.requirements||running" @click="addCandidate">加入候选</el-button></div></details><p class="muted">{{pending.length}} 个候选待审；继续调研只使用当前选定技术或位置覆盖缺口。</p>
</section>
</div>
<section class="surface study-card planning-card"><header><h2>③ 选择你要学什么</h2><el-tag v-if="track.collection_policy==='strict_count'&&!countMet">严格要求：还差 {{Math.max(0,track.target_count-track.approved_count)}} 个项目</el-tag><el-tag v-else-if="!countMet" type="info">数量为偏好，可确认开始已覆盖范围</el-tag></header><div class="intent-options"><button v-for="(label,key) in intentNames" :key="key" :class="{chosen:intent===key}" @click="intent=key">{{label}}</button></div><div class="plan-inputs"><el-select v-model="reqKeys" multiple placeholder="选择技术需求"><el-option v-for="r in track.requirements?.requirements" :key="r.key" :value="r.key" :label="r.technology+' · '+r.depth"/></el-select><el-select v-model="reviewIds" multiple placeholder="选择审核通过的参考项目"><el-option v-for="p in approved" :key="p.id" :value="p.id" :label="p.candidate.full_name"/></el-select></div><el-input v-model="focus" type="textarea" :rows="2" :placeholder="intent==='function'?'例如：工具注册函数的参数校验与调用实现':intent==='technical_design'?'例如：Agent 工具权限和状态恢复设计':'补充你的学习目标、已有基础或时间安排（可选）'"/>
<p v-if="reqKeys.length" class="muted">所选目标的位置覆盖 {{selectedCoverage.covered.length}} / {{reqKeys.length}}。{{selectedCoverage.missing.length?'缺少代码依据：'+selectedCoverage.missing.join('、'):'全部已有代码位置；语义支持仍未独立核验。'}}</p>
<div v-if="['function','technical_design'].includes(intent)" class="focus-index"><el-button :disabled="!focus.trim()||!reviewIds.length" :loading="searchBusy" @click="findFocus">从所选项目检索实现索引</el-button><el-select v-if="focusHits.length" v-model="focusChunk" clearable placeholder="选择具体函数 / 实现片段"><el-option v-for="h in focusHits" :key="h.chunk_id" :value="h.chunk_id" :label="h.symbol+' · '+h.path+':'+h.start_line"/></el-select><small>函数学习必须选定实际代码；非 Python 代码按文本窗口定位。</small></div>
<div class="plan-actions"><el-button type="primary" :disabled="!ready||running||!reqKeys.length||!reviewIds.length||(['function','technical_design'].includes(intent)&&!focus.trim())||(intent==='function'&&!focusChunk)" :loading="busy" @click="run('plan')">制定模块化计划</el-button><span class="muted">不自动更改已有计划；新计划保留为独立版本。</span></div>
<div v-if="latestPlan" class="plan-result"><div><h3>{{latestPlan.title}}</h3><el-tag v-if="latestPlan.synthetic" type="warning">离线模拟</el-tag><p>{{latestPlan.summary}}</p><small>{{latestPlan.modules.length}} 个分级模块 · {{intentNames[latestPlan.intent.kind]}}</small></div><el-button type="primary" @click="learn">进入学习 · 计划 / 代码 / Agent</el-button></div>
</section>
<details class="budget"><summary>本轮模型设置</summary><span>gpt-6.1-sol · low · 提取 / 计划各 1 次；单项审核默认 1 次，增加后才启用模型多轮补读</span><el-input-number v-model="tokenCap" :min="512" :max="8192" :step="512"/><small>输出 token 上限；导师问答单独设置。金额取决于供应商。</small><label>每候选审核模型调用上限（含最终报告）</label><el-input-number v-model="reviewCalls" :min="1" :max="3"/><small>1 次：先确定性补读，再生成报告；2–3 次：允许模型选择补读。批次最多 {{1+5*reviewCalls}} 次，启动前再次确认。</small></details>
</template>
<el-dialog v-model="createOpen" title="从人工需求开始" width="560px"><el-form label-position="top"><el-form-item label="学习方向名称"><el-input v-model="name"/></el-form-item><el-form-item label="人工导入的正文（最多 5 份，固定当前版本）"><el-select v-model="sourceIds" multiple :multiple-limit="5" style="width:100%"><el-option v-for="s in sources" :key="s.source_id" :value="s.source_id" :label="s.title||'未命名资料'"/></el-select></el-form-item><router-link to="/sources" @click="createOpen=false">先导入岗位正文 →</router-link><el-form-item label="项目收集数量"><el-input-number v-model="target" :min="1" :max="10"/></el-form-item><el-form-item label="数量策略"><el-select v-model="newPolicy"><el-option label="覆盖优先，数量作为偏好" value="coverage"/><el-option label="必须达到指定数量" value="strict_count"/></el-select></el-form-item></el-form><template #footer><el-button @click="createOpen=false">取消</el-button><el-button type="primary" :disabled="!name.trim()||!sourceIds.length" :loading="busy" @click="create">创建方向</el-button></template></el-dialog>
</template>
<style scoped>
.table-scroll{overflow:auto}.coverage table{width:100%;border-collapse:collapse;font-size:12px}.coverage th,.coverage td{padding:8px;border:1px solid #dce5dc;text-align:left}.coverage{margin:14px 0}

.track-bar,.flow-strip,.run-status,.target-setting,.project-title,.plan-actions,.plan-result,.existing{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.track-bar{margin-bottom:20px}.track-bar .el-select{width:310px}.track-bar a,.run-status a{font-size:13px;color:#357258}.flow-strip{padding:18px;background:#eaf2ec;border-radius:12px;margin:16px 0;color:#89978e;font-size:13px}.flow-strip .done{color:#2b6c4c;font-weight:600}.flow-strip b{font-weight:400;color:#a5b5a9}.run-status{padding:10px 0;font-size:13px}.study-columns{display:grid;grid-template-columns:minmax(0,.85fr) minmax(0,1.15fr);gap:18px;align-items:start}.study-card{padding:22px;margin-bottom:18px}.study-card header{display:flex;justify-content:space-between;align-items:center;gap:10px}.study-card h2{font-size:17px;margin:0}.muted,small{font-size:12px;color:#75837a;line-height:1.8}.source-chips span{display:inline-block;margin:3px;padding:4px 8px;background:#f1f5f2;font-size:12px;border-radius:5px}.requirement,.project{border-top:1px solid #e6ece7;margin-top:16px;padding-top:16px;font-size:13px}.requirement>div{display:flex;align-items:center;gap:9px}.requirement p,.project p{line-height:1.8;margin:9px 0}.project-title a{color:#2e6550;font-weight:600;text-decoration:none;overflow-wrap:anywhere}.project>div:last-child{margin-top:10px}.project small{display:block}.audit{background:#f5f8f5;padding:14px;border-radius:8px}.audit .el-button{white-space:normal;height:auto;line-height:1.7;text-align:left}blockquote{border-left:3px solid #b4cdbb;margin:10px 0;padding:8px 12px;background:#f4f7f4;white-space:pre-wrap}blockquote small{display:block}summary{cursor:pointer;font-size:12px;color:#5a7462}.target-setting{margin:16px 0;font-size:12px}.existing{margin-top:12px}.existing .el-select{flex:1}.intent-options{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}.intent-options button{padding:18px 8px;border:1px solid #dce5dc;background:#f9fbf9;border-radius:9px;color:#4c6758;cursor:pointer}.intent-options .chosen{border-color:#3e7955;background:#e7f3e9;color:#28603c;font-weight:600}.plan-inputs{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0}.focus-index{display:grid;grid-template-columns:auto 1fr;gap:10px;margin-top:12px}.focus-index small{grid-column:1/-1}.plan-actions{margin:16px 0}.plan-result{justify-content:space-between;background:#f0f6f0;padding:20px;border-radius:10px}.plan-result h3{margin-top:0}.budget{font-size:12px;padding:10px;color:#75837a}.budget>span,.budget>small{display:block;margin:10px 0}@media(max-width:1100px){.study-columns{grid-template-columns:1fr}}@media(max-width:650px){.intent-options,.plan-inputs{grid-template-columns:1fr 1fr}.study-card{padding:15px}.plan-inputs{grid-template-columns:1fr}}
</style>
