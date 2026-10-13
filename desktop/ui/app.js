const $ = id => document.getElementById(id)
const invoke = window.__TAURI__?.core?.invoke
let refreshing=false, starting=false, opened=false, startupError=''
const names={running:'运行中',missing:'未创建',exited:'已停止',created:'已创建',restarting:'重启中',unknown:'待检查',paused:'已暂停'}
function render(value){
  $('badge').textContent=value.ready?'已就绪':value.docker==='running'?'部分服务待就绪':'Docker 待就绪'
  $('badge').className='badge '+(value.ready?'good':'pending')
  $('message').textContent=startupError||value.message
  $('project').textContent='项目目录：'+(value.project_dir??'未找到')
  $('api').textContent='业务接口 · '+(value.api_ready?'就绪':'未就绪')
  $('ui').textContent='页面连接 · '+(value.ui_ready?'就绪':'未就绪')
  $('sandbox').textContent='沙箱网络隔离 · '+(value.sandbox_isolated?'无网络':'未确认')
  $('open').disabled=!invoke||!(value.api_ready&&value.ui_ready)||starting
  $('services').replaceChildren(...value.services.map(service=>{
    const card=document.createElement('article'),title=document.createElement('h2'),state=document.createElement('p'),note=document.createElement('small')
    title.textContent=service.name
    state.textContent=names[service.state]??service.state
    state.className=service.state==='running'&&service.health!=='unhealthy'?'ok':'wait'
    note.textContent=service.health==='healthy'?'健康检查通过':service.health==='unhealthy'?'健康检查未通过':service.health==='starting'?'健康检查中':service.state==='running'?'容器进程运行':'等待启动'
    card.append(title,state,note);return card
  }))
  $('updated').textContent='最近检查：'+new Date().toLocaleTimeString('zh-CN')
}
async function refresh(){
  if(refreshing)return
  refreshing=true;$('refresh').disabled=true
  try{
    const value=invoke?await invoke('desktop_status'):await fetch('status-preview.json',{cache:'no-store'}).then(r=>r.json())
    render(value);return value
  }catch(error){$('message').textContent=invoke?String(error):'此页面需要通过 CareerForge.exe 启动。';$('badge').textContent='连接待检查'}
  finally{refreshing=false;$('refresh').disabled=false}
}
async function open(){
  try{await invoke('open_workspace');opened=true}catch(error){$('message').textContent=String(error)}
}
async function start(){
  if(!invoke||starting)return
  startupError='';starting=true;$('start').disabled=true;$('open').disabled=true;$('badge').textContent='正在启动';$('message').textContent='正在准备本机服务，Docker 首次启动可能需要一两分钟…'
  try{const message=await invoke('start_services');$('message').textContent=message}
  catch(error){startupError=String(error);$('message').textContent=startupError;$('badge').textContent='需要处理'}
  finally{starting=false;$('start').disabled=false}
  const value=await refresh()
  if(value?.ready&&!opened)await open()
}
$('refresh').onclick=refresh;$('start').onclick=start;$('open').onclick=open
if(!invoke){$('start').disabled=true;$('open').disabled=true}
refresh().then(()=>{if(invoke)start()})
setInterval(()=>{if(!starting)refresh()},15000)
