<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, mutation } from '../api/client'
export type NativeEdit={path:string;before:string|null;after:string|null;reason?:string}
type Project={project_id:string;name:string;directory:string;files:string[];limited:boolean}
type LocalFile={path:string;content:string;hash:string|null}
type Receipt={operation_id:string;backup_directory:string;files:LocalFile[]}
type Mirror={workspace_id:string;revision:number;files:Record<string,string>}
const props=defineProps<{workspace:Mirror|null;practiceBound?:boolean;independent?:boolean;canSwitch:()=>Promise<boolean>}>()
const emit=defineEmits<{bound:[id:string];synced:[id:string];retry:[id:string]}>()
const invoke=(window as unknown as {__TAURI__?:{core:{invoke:<T>(command:string,args?:unknown)=>Promise<T>}}}).__TAURI__?.core.invoke
const project=ref<Project|null>(null),boundId=ref(''),current=ref<LocalFile|null>(null),editor=ref(''),filter=ref(''),busy=ref(false),agentPaths=ref<string[]>([]),runPaths=ref<string[]>([]),backup=ref(''),pending=ref<Receipt|null>(null)
const nativeReady=ref(false)
const write=mutation(),hashes=ref<Record<string,string>>({})
const dirty=computed(()=>!!current.value&&(current.value.hash===null||editor.value!==current.value.content))
const connected=computed(()=>!!project.value&&props.workspace?.workspace_id===boundId.value)
const paths=computed(()=>project.value?.files.filter(p=>p.toLowerCase().includes(filter.value.toLowerCase()))??[])
async function call<T>(command:string,args:Record<string,unknown>={}){if(!invoke||!project.value)throw new Error('请先选择本机项目目录');return invoke<T>(command,{projectId:project.value.project_id,...args})}
async function canLeave(){if(!dirty.value)return true;try{await ElMessageBox.confirm('本机文件有未保存修改，是否放弃编辑内容？','未保存的代码',{confirmButtonText:'放弃修改',cancelButtonText:'继续编辑'});return true}catch{return false}}
async function disconnect(){if(project.value&&invoke)await invoke('local_project_close',{projectId:project.value.project_id}).catch(()=>{});project.value=null;current.value=null;boundId.value='';pending.value=null;hashes.value={}}
async function choose(){if(props.independent){ElMessage.warning('独立尝试使用新建练习副本，不绑定本机项目');return}if(!invoke||!await props.canSwitch())return;busy.value=true;try{
  const selected=await invoke<Project|null>('local_project_pick');if(!selected)return
  project.value=selected;current.value=null;editor.value='';hashes.value={};pending.value=null;agentPaths.value=[];runPaths.value=[]
  const value=props.practiceBound&&props.workspace?props.workspace:await write<{workspace_id:string}>('/studio/workspaces','POST',{name:('[本机] '+selected.name).slice(0,100),files:{}})
  boundId.value=value.workspace_id;emit('bound',value.workspace_id)
  ElMessage.success('目录已打开；读取和保存留在本机，选择文件并发送时才交给 Agent')
}catch(e){ElMessage.error(String(e))}finally{busy.value=false}}
async function tree(){if(!project.value)return;try{project.value=await call<Project>('local_project_tree')}catch(e){ElMessage.error(String(e))}}
async function open(path:string){if(!await canLeave())return;try{current.value=await call<LocalFile>('local_project_read',{path});editor.value=current.value.content;if(!agentPaths.value.length)agentPaths.value=[path];if(!runPaths.value.length)runPaths.value=[path]}catch(e){ElMessage.error(String(e))}}
async function newFile(){if(!await canLeave())return;try{const {value}=await ElMessageBox.prompt('输入项目内相对路径，例如 src/example.py','新建本机文件');if(project.value?.files.includes(value))throw new Error('文件已存在，请从列表打开');current.value={path:value,content:'',hash:null};editor.value=''}catch(e){if(e!=='cancel'&&e!=='close')ElMessage.error(String(e))}}
async function save(){if(!current.value||pending.value)return;busy.value=true;try{const receipt=await call<Receipt>('local_project_write',{edit:{path:current.value.path,before:current.value.hash===null?null:current.value.content,after:editor.value,expected_hash:current.value.hash}});current.value=receipt.files[0]!;editor.value=current.value.content;backup.value=receipt.backup_directory;await tree();ElMessage.success('已直接保存到本机文件')}catch(e){ElMessage.error(String(e))}finally{busy.value=false}}
async function prepareSnapshot(kind:'agent'|'command'|'submission'|'manual'){
  if(!connected.value||!props.workspace)throw new Error('请先打开本机项目对应的工作区')
  if(dirty.value)throw new Error('请先保存本机代码')
  if(pending.value)throw new Error('先完成已写入补丁的记录同步')
  const selected=kind==='agent'?agentPaths.value:runPaths.value
  if(!selected.length)throw new Error('请选择本次使用的文件')
  if(kind==='agent'&&selected.length>3)throw new Error('Agent 每次最多接收 3 个文件')
  if(kind==='agent')await ElMessageBox.confirm('将发送以下文件的已保存内容给当前配置的模型：\n'+selected.join('\n')+'\n其余本机文件不会上传。','确认 Agent 上下文',{confirmButtonText:'发送这些文件',cancelButtonText:'取消'})
  const snapshot=await call<{files:Record<string,string>;hashes:Record<string,string>}>('local_project_snapshot',{paths:selected})
  const latest=await api<Mirror>('/studio/workspaces/'+boundId.value)
  await write('/studio/workspaces/'+boundId.value+'/snapshot','PUT',{expected_revision:latest.revision,files:snapshot.files})
  hashes.value=snapshot.hashes;emit('synced',boundId.value)
  return {paths:selected.slice(),files:snapshot.files}
}
async function applyPatch(id:string,edits:NativeEdit[]){
  if(!connected.value||dirty.value)throw new Error('请先保存本机文件并确认当前目录')
  if(pending.value){if(pending.value.operation_id!==id)throw new Error('先同步上一份已写入补丁的记录');return pending.value}
  const receipt=await call<Receipt>('local_project_patch',{patchId:id,edits:edits.map(e=>({...e,expected_hash:e.before===null?null:hashes.value[e.path]??null}))})
  pending.value=receipt;backup.value=receipt.backup_directory
  const updated=receipt.files.find(f=>f.path===current.value?.path);if(updated){current.value=updated;editor.value=updated.content}
  await tree();return receipt
}
async function syncManual(){try{await prepareSnapshot('manual');ElMessage.success('所选文件快照已同步')}catch(e){ElMessage.error(String(e))}}
function recorded(){pending.value=null}
function tab(e:KeyboardEvent){if(e.key!=='Tab')return;e.preventDefault();const el=e.target as HTMLTextAreaElement,pos=el.selectionStart;editor.value=editor.value.slice(0,pos)+'  '+editor.value.slice(el.selectionEnd);queueMicrotask(()=>{el.selectionStart=el.selectionEnd=pos+2})}
watch(()=>props.workspace?.workspace_id,id=>{if(boundId.value&&id!==boundId.value)void disconnect()})
onMounted(async()=>{if(invoke)try{await invoke('local_project_capabilities');nativeReady.value=true}catch{ElMessage.warning('本机 IDE 需要更新后的桌面程序，请退出并重新启动 CareerForge')}})
onBeforeUnmount(()=>{void disconnect()})
defineExpose({dirty,connected,choose,disconnect,prepareSnapshot,applyPatch,recorded,paths:agentPaths,pendingId:computed(()=>pending.value?.operation_id??'')})
</script>
<template>
  <section class="local-ide">
    <div class="toolbar"><el-button type="primary" :loading="busy" :disabled="!nativeReady" @click="choose">选择本机项目目录</el-button><el-button v-if="project" @click="tree">刷新文件列表</el-button></div>
    <p v-if="project" class="muted">{{project.directory}}</p><el-empty v-else description="选择要学习实践的项目，代码直接读写本机目录。"/>
    <template v-if="project"><small class="muted">源码和 UTF-8 文本 · 单文件 100 KB。私密目录、依赖、链接和过大文件不显示。{{project.limited?'列表已达扫描上限，请选择更小目录。':''}}</small>
      <el-alert v-if="pending" title="代码已写入本机，但应用记录尚未同步；重试只同步记录，不重复写文件。" type="warning" :closable="false"/><el-button v-if="pending" @click="emit('retry',pending.operation_id)">重试保存应用记录</el-button>
      <div class="local-layout"><aside><el-input v-model="filter" placeholder="查找文件"/><el-button link @click="newFile">＋ 文件</el-button><button v-for="path in paths" :key="path" :class="{active:current?.path===path}" @click="open(path)">{{path}}</button></aside>
        <div class="editor"><div class="toolbar"><span>{{current?.path??'选择文件'}}{{dirty?' ●':''}}</span><el-button type="primary" :disabled="!dirty||!!pending" :loading="busy" @click="save">保存到本机</el-button><el-button v-if="current" link @click="open(current.path)">重新读取</el-button></div><textarea v-model="editor" :disabled="!current" aria-label="本机项目代码编辑器" spellcheck="false" @keydown="tab" @keydown.ctrl.s.prevent="save" @keydown.meta.s.prevent="save"/></div>
      </div>
      <div class="context"><span>交给 Agent 的文件</span><el-select v-model="agentPaths" multiple filterable :multiple-limit="3" placeholder="最多 3 个"><el-option v-for="path in project.files" :key="path" :label="path" :value="path"/></el-select></div>
      <details><summary>沙箱与成果快照文件</summary><p>命令只在原有无网络容器内执行以下文件副本，仍需逐条确认；不会在本机目录执行。成果提交也只覆盖这些文件。</p><el-select v-model="runPaths" multiple filterable :multiple-limit="100" placeholder="选择本次需要的文件"><el-option v-for="path in project.files" :key="path" :label="path" :value="path"/></el-select><el-button @click="syncManual">同步所选文件快照</el-button></details>
      <small v-if="backup" class="muted">最近写入前备份：{{backup}}</small>
    </template>
  </section>
</template>
<style scoped>
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.muted{display:block;color:#74847d;font-size:12px;overflow-wrap:anywhere;line-height:1.8}.local-layout{display:grid;grid-template-columns:170px minmax(0,1fr);border:1px solid #dbe5de;border-radius:8px;margin:12px 0;overflow:hidden}.local-layout aside{background:#f3f7f4;max-height:560px;overflow:auto;padding:8px}.local-layout aside>button{display:block;text-align:left;border:0;background:none;width:100%;padding:8px;font-size:12px;overflow-wrap:anywhere;cursor:pointer}.local-layout aside>button.active{background:#dcece1}.editor{min-width:0}.editor .toolbar{padding:0 10px}.editor textarea{display:block;width:100%;min-height:490px;resize:vertical;border:0;border-top:1px solid #dbe5de;padding:12px;background:#172723;color:#e3eee7;font:13px/1.7 Consolas,monospace;tab-size:2;white-space:pre;overflow:auto}.context{display:flex;gap:12px;align-items:center;font-size:12px}.context .el-select{flex:1}details{font-size:12px;margin-top:14px;line-height:1.8}summary{cursor:pointer}details .el-select{margin:8px 0}

/* Keep the existing native bridge; only fit its editor into the V4.2 split view. */
.local-ide{display:flex;flex-direction:column;min-height:0;height:100%}.local-layout{flex:1;min-height:180px;grid-template-columns:130px minmax(0,1fr);border-color:#e6eaf2}.local-layout aside{max-height:none;min-height:0;background:#f6f8fc}.local-layout aside>button{font-size:10px;color:#7b89a1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.local-layout aside>button.active{background:#eeebff;color:#6658e5}.editor{display:flex;flex-direction:column;min-height:0;overflow:hidden}.editor textarea{flex:1;min-height:160px;resize:none;background:#111c2e;color:#c7d7e9;border-color:#e6eaf2;font:12px/22px Consolas,monospace}.toolbar{flex:none;margin:7px 0;gap:6px}.toolbar .el-button{font-size:10px}.toolbar>span{font-size:10px;color:#98a4b7}.muted,details{font-size:10px;color:#98a4b7}.context{font-size:10px;flex:none}.context .el-select{min-width:90px}details{flex:none;max-height:140px;overflow:auto}
@media(max-width:1400px){.local-layout{grid-template-columns:100px minmax(0,1fr)}}
</style>
