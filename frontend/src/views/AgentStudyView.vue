<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiPage, mutation } from '../api/client'
import { type StudyTrack, intentNames, roleNames, activeStates } from '../types/study'
const router=useRouter(),write=mutation()
const tracks=ref<{track_id:string;name:string;available:boolean}[]>([]),id=ref(''),track=ref<StudyTrack|null>(null)
const sources=ref<{source_id:string;title:string;read_status:string}[]>([]),sourceIds=ref<string[]>([]),name=ref('Agent 开发'),target=ref(5)
const createOpen=ref(false),busy=ref(false),showReview=ref(''),tokenCap=ref(4096)
const intent=ref('learn_project'),focus=ref(''),reqKeys=ref<string[]>([]),reviewIds=ref<string[]>([])
const focusChunk=ref(''),focusHits=ref<{chunk_id:string;symbol:string;path:string;start_line:number}[]>([]),searchBusy=ref(false)
const existing=ref<{repository_snapshot_id:string;name:string}[]>([]),existingId=ref('')
const running=computed(()=>!!track.value?.active_run&&activeStates.includes(track.value.active_run.status))
const approved=computed(()=>track.value?.approved_projects??[])
const ready=computed(()=>!!track.value&&(track.value.approved_count>=track.value.target_count))
const reviewed=computed(()=>new Map(track.value?.reviews.map(r=>[r.candidate_id,r])??[]))
const pending=computed(()=>track.value?.candidates.filter(c=>!reviewed.value.has(c.id))??[])
const latestPlan=computed(()=>track.value?.plans.at(-1))
let timer:ReturnType<typeof setTimeout>|undefined,alive=true,loadNo=0
async function refresh(){
 clearTimeout(timer)
 if(!id.value)return
 const serial=++loadNo
 try{const result=await api<StudyTrack>('/study/tracks/'+id.value);if(serial!==loadNo)return;track.value=result;target.value=result.target_count
 reqKeys.value=reqKeys.value.filter(k=>result.requirements?.requirements.some(r=>r.key===k))
 reviewIds.value=reviewIds.value.filter(k=>result.approved_projects.some(r=>r.id===k))
 }catch(e){ElMessage.error(String(e));track.value=null}
 if(alive&&running.value)timer=setTimeout(refresh,2500)
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
 const result=await write<StudyTrack>('/study/tracks','POST',{name:name.value,source_snapshot_ids:versions,target_count:target.value})
 createOpen.value=false;await load();id.value=result.track_id;await refresh()
 }catch(e){ElMessage.error(String(e))}finally{busy.value=false}
}
async function run(role:string,candidateId?:string){
 if(!track.value)return
 try{
 await ElMessageBox.confirm(`${roleNames[role]}：最多 ${role==='discover'?6:1} 次模型调用，输出上限 ${tokenCap.value} token。${role==='discover'?'另执行最多 3 次 GitHub 公开仓库搜索并审核最多 5 个候选，达到目标即停止。':role==='review'?'读取候选的固定版本文档和代码，不执行项目。':''} 供应商按实际用量计费。`,'启动 Agent',{confirmButtonText:'启动本轮',cancelButtonText:'取消'})
 busy.value=true
 const body:Record<string,unknown>={expected_revision:track.value.revision,role,max_output_tokens:tokenCap.value,model_call_limit:1}
 if(candidateId)body.candidate_id=candidateId
 if(role==='discover')body.auto_review_count=5
 if(role==='plan')body.intent={kind:intent.value,focus:focus.value,focus_chunk_id:focusChunk.value||null,requirement_keys:reqKeys.value,project_review_ids:reviewIds.value}
 await write('/study/tracks/'+id.value+'/runs','POST',body);await refresh()
 }catch(e){if(e!=='cancel'&&e!=='close')ElMessage.error(String(e))}finally{busy.value=false}
}
async function settings(){
 if(!track.value)return
 try{track.value=await api('/study/tracks/'+id.value,'PUT',{expected_revision:track.value.revision,target_count:target.value})}catch(e){ElMessage.error(String(e))}
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
<div class="flow-strip"><span :class="{done:track.sources.length}">人工需求</span><b>→</b><span :class="{done:track.requirements}">技术要求</span><b>→</b><span :class="{done:track.candidates.length}">研究与审核</span><b>→</b><span :class="{done:ready}">项目库 {{track.approved_count}} / {{track.target_count}}</span><b>→</b><span :class="{done:latestPlan}">模块化学习</span></div>
<div v-if="track.active_run" class="run-status"><span>{{roleNames[track.active_run.role]}} · {{track.active_run.status}}</span><el-button v-if="running" link @click="cancel">停止本轮</el-button><router-link to="/runs">查看运行记录</router-link></div>
<el-alert v-if="track.active_run?.status==='failed'" title="本轮未发布结果。请在运行记录查看原因；系统没有自动追加收费重试。" type="warning" :closable="false"/>
<div class="study-columns">
<section class="surface study-card"><header><h2>① 技术需求</h2><el-button :disabled="running" :loading="busy" @click="run('extract')">{{track.requirements?'重新提取':'提取需求'}}</el-button></header><p class="muted">了解 / 熟悉 / 掌握是目标深度。推断会单独标注。</p><p class="source-chips"><span v-for="s in track.sources" :key="s.source_snapshot_id">{{s.title}}</span></p>
<el-empty v-if="!track.requirements" description="尚未提取技术要求"/>
<el-tag v-if="track.requirements?.synthetic" type="warning">离线模拟</el-tag>
<article v-for="r in track.requirements?.requirements" :key="r.key" class="requirement"><div><strong>{{r.technology}}</strong><el-tag :type="r.depth==='掌握'?'success':'info'" size="small">{{r.depth}}</el-tag><small>{{r.depth_basis==='explicit'?'原文明确':'模型推断'}}</small></div><p>{{r.rationale}}</p><details><summary>原文依据 · {{r.evidence.length}}</summary><blockquote v-for="(e,i) in r.evidence" :key="i">{{e.quote}}<small>{{e.source_title}}</small></blockquote><small>检索词：{{r.keywords.join('、')}}</small></details></article>
</section>
<section class="surface study-card"><header><h2>② 开源项目研究与审核</h2><el-button type="primary" plain :disabled="!track.requirements||running" :loading="busy" @click="run('discover')">调研并审核一批</el-button></header><p class="muted">先发现候选，再读取固定版本代码审核。强相关且质量条件满足才入库。</p>
<div class="target-setting"><span>入库目标</span><el-input-number v-model="target" :min="1" :max="10" size="small"/><el-button size="small" :disabled="running||target===track.target_count" @click="settings">保存</el-button><strong>{{track.approved_count}} 个已入库</strong></div>
<el-empty v-if="!track.candidates.length" description="等待技术关键词调研，或送审已有项目"/>
<article v-for="c in track.candidates" :key="c.id" class="project"><div class="project-title"><a :href="c.github_url" target="_blank" rel="noopener noreferrer">{{c.full_name}}</a><el-tag size="small" :type="reviewed.get(c.id)?.admitted?'success':'info'">{{reviewed.get(c.id)?.admitted?'已入库':reviewed.get(c.id)?({strong:'相关但未入库',weak:'弱相关',uncertain:'待补证据'}[reviewed.get(c.id)!.verdict as 'strong'|'weak'|'uncertain']):'候选待审'}}</el-tag></div><p>{{c.description}}</p><small>★ {{c.stars??'未记录'}} · {{c.license??'许可证待核对'}} · {{c.origin==='user_import'?'人工项目':'GitHub 检索'}}</small><div><el-button size="small" :disabled="running" @click="run('review',c.id)">{{reviewed.has(c.id)?'重新审核':'审核此项目'}}</el-button><el-button v-if="reviewed.has(c.id)" link @click="showReview=showReview===c.id?'':c.id">审核依据</el-button></div>
<div v-if="showReview===c.id&&reviewed.get(c.id)" class="audit"><p>{{reviewed.get(c.id)?.summary}}</p><small>技术栈：{{reviewed.get(c.id)?.technology_stack.join('、')}}</small><ul><li v-for="q in reviewed.get(c.id)?.quality_notes" :key="q">{{q}}</li></ul><el-button v-for="h in reviewed.get(c.id)?.highlights" :key="h.citation_id" link @click="router.push({path:'/studio',query:{citation:h.citation_id}})">{{h.requirement_key}} → {{h.path}}:{{h.start_line}}</el-button><p v-for="l in reviewed.get(c.id)?.limitations" :key="l" class="muted">{{l}}</p></div></article>
<details><summary>送审已导入的开源项目</summary><div class="existing"><el-select v-model="existingId" placeholder="选择已有固定版本"><el-option v-for="p in existing" :key="p.repository_snapshot_id" :value="p.repository_snapshot_id" :label="p.name"/></el-select><el-button :disabled="!existingId||!track.requirements||running" @click="addCandidate">加入候选</el-button></div></details><p class="muted">{{pending.length}} 个候选待审；数量不够可继续一轮调研。</p>
</section>
</div>
<section class="surface study-card planning-card"><header><h2>③ 选择你要学什么</h2><el-tag v-if="!ready">还差 {{Math.max(0,track.target_count-track.approved_count)}} 个审核项目</el-tag></header><div class="intent-options"><button v-for="(label,key) in intentNames" :key="key" :class="{chosen:intent===key}" @click="intent=key">{{label}}</button></div><div class="plan-inputs"><el-select v-model="reqKeys" multiple placeholder="选择技术需求"><el-option v-for="r in track.requirements?.requirements" :key="r.key" :value="r.key" :label="r.technology+' · '+r.depth"/></el-select><el-select v-model="reviewIds" multiple placeholder="选择审核通过的参考项目"><el-option v-for="p in approved" :key="p.id" :value="p.id" :label="p.candidate.full_name"/></el-select></div><el-input v-model="focus" type="textarea" :rows="2" :placeholder="intent==='function'?'例如：工具注册函数的参数校验与调用实现':intent==='technical_design'?'例如：Agent 工具权限和状态恢复设计':'补充你的学习目标、已有基础或时间安排（可选）'"/>
<div v-if="['function','technical_design'].includes(intent)" class="focus-index"><el-button :disabled="!focus.trim()||!reviewIds.length" :loading="searchBusy" @click="findFocus">从所选项目检索实现索引</el-button><el-select v-if="focusHits.length" v-model="focusChunk" clearable placeholder="选择具体函数 / 实现片段"><el-option v-for="h in focusHits" :key="h.chunk_id" :value="h.chunk_id" :label="h.symbol+' · '+h.path+':'+h.start_line"/></el-select><small>函数学习必须选定实际代码；非 Python 代码按文本窗口定位。</small></div>
<div class="plan-actions"><el-button type="primary" :disabled="!ready||running||!reqKeys.length||!reviewIds.length||(['function','technical_design'].includes(intent)&&!focus.trim())||(intent==='function'&&!focusChunk)" :loading="busy" @click="run('plan')">制定模块化计划</el-button><span class="muted">不自动更改已有计划；新计划保留为独立版本。</span></div>
<div v-if="latestPlan" class="plan-result"><div><h3>{{latestPlan.title}}</h3><el-tag v-if="latestPlan.synthetic" type="warning">离线模拟</el-tag><p>{{latestPlan.summary}}</p><small>{{latestPlan.modules.length}} 个分级模块 · {{intentNames[latestPlan.intent.kind]}}</small></div><el-button type="primary" @click="learn">进入学习 · 计划 / 代码 / Agent</el-button></div>
</section>
<details class="budget"><summary>本轮模型设置</summary><span>gpt-6.1-sol · low · 提取 / 计划 / 单项审核各 1 次；调研批次最多 6 次</span><el-input-number v-model="tokenCap" :min="512" :max="4096" :step="512"/><small>输出 token 上限；导师问答单独设置。金额取决于供应商。</small></details>
</template>
<el-dialog v-model="createOpen" title="从人工需求开始" width="560px"><el-form label-position="top"><el-form-item label="学习方向名称"><el-input v-model="name"/></el-form-item><el-form-item label="人工导入的正文（最多 5 份，固定当前版本）"><el-select v-model="sourceIds" multiple :multiple-limit="5" style="width:100%"><el-option v-for="s in sources" :key="s.source_id" :value="s.source_id" :label="s.title||'未命名资料'"/></el-select></el-form-item><router-link to="/sources" @click="createOpen=false">先导入岗位正文 →</router-link><el-form-item label="审核入库目标数量"><el-input-number v-model="target" :min="1" :max="10"/></el-form-item></el-form><template #footer><el-button @click="createOpen=false">取消</el-button><el-button type="primary" :disabled="!name.trim()||!sourceIds.length" :loading="busy" @click="create">创建方向</el-button></template></el-dialog>
</template>
<style scoped>
.track-bar,.flow-strip,.run-status,.target-setting,.project-title,.plan-actions,.plan-result,.existing{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.track-bar{margin-bottom:20px}.track-bar .el-select{width:310px}.track-bar a,.run-status a{font-size:13px;color:#357258}.flow-strip{padding:18px;background:#eaf2ec;border-radius:12px;margin:16px 0;color:#89978e;font-size:13px}.flow-strip .done{color:#2b6c4c;font-weight:600}.flow-strip b{font-weight:400;color:#a5b5a9}.run-status{padding:10px 0;font-size:13px}.study-columns{display:grid;grid-template-columns:minmax(0,.85fr) minmax(0,1.15fr);gap:18px;align-items:start}.study-card{padding:22px;margin-bottom:18px}.study-card header{display:flex;justify-content:space-between;align-items:center;gap:10px}.study-card h2{font-size:17px;margin:0}.muted,small{font-size:12px;color:#75837a;line-height:1.8}.source-chips span{display:inline-block;margin:3px;padding:4px 8px;background:#f1f5f2;font-size:12px;border-radius:5px}.requirement,.project{border-top:1px solid #e6ece7;margin-top:16px;padding-top:16px;font-size:13px}.requirement>div{display:flex;align-items:center;gap:9px}.requirement p,.project p{line-height:1.8;margin:9px 0}.project-title a{color:#2e6550;font-weight:600;text-decoration:none;overflow-wrap:anywhere}.project>div:last-child{margin-top:10px}.project small{display:block}.audit{background:#f5f8f5;padding:14px;border-radius:8px}.audit .el-button{white-space:normal;height:auto;line-height:1.7;text-align:left}blockquote{border-left:3px solid #b4cdbb;margin:10px 0;padding:8px 12px;background:#f4f7f4;white-space:pre-wrap}blockquote small{display:block}summary{cursor:pointer;font-size:12px;color:#5a7462}.target-setting{margin:16px 0;font-size:12px}.existing{margin-top:12px}.existing .el-select{flex:1}.intent-options{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}.intent-options button{padding:18px 8px;border:1px solid #dce5dc;background:#f9fbf9;border-radius:9px;color:#4c6758;cursor:pointer}.intent-options .chosen{border-color:#3e7955;background:#e7f3e9;color:#28603c;font-weight:600}.plan-inputs{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0}.focus-index{display:grid;grid-template-columns:auto 1fr;gap:10px;margin-top:12px}.focus-index small{grid-column:1/-1}.plan-actions{margin:16px 0}.plan-result{justify-content:space-between;background:#f0f6f0;padding:20px;border-radius:10px}.plan-result h3{margin-top:0}.budget{font-size:12px;padding:10px;color:#75837a}.budget>span,.budget>small{display:block;margin:10px 0}@media(max-width:1100px){.study-columns{grid-template-columns:1fr}}@media(max-width:650px){.intent-options,.plan-inputs{grid-template-columns:1fr 1fr}.study-card{padding:15px}.plan-inputs{grid-template-columns:1fr}}
</style>
