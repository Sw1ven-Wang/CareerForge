"use strict";

const icons = {
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M8 10h8M8 14h5"/>',
  book: '<path d="M12 7v14m0-14C9 4 5 4 2 5v14c4-1 7-1 10 2 3-3 6-3 10-2V5c-3-1-7-1-10 2Z"/>',
  route: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><path d="M6 7v6a4 4 0 0 0 8 0V9a4 4 0 0 1 8 0v4M18 17v-4"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 11-2l3 3M4 16l3 3a7 7 0 0 0 11-2"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  spark: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z"/>',
  code: '<path d="m8 5-6 7 6 7m8-14 6 7-6 7m-3-16-2 20"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  send: '<path d="m4 11 16-7-7 16-2-7-7-2Zm7 2 9-9"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
  file: '<path d="M14 2H5v20h14V7l-5-5Z"/><path d="M14 2v6h5M8 12h8M8 16h6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const titles = {chat:"学习对话", knowledge:"项目知识库", plans:"我的学习计划", goals:"岗位与目标", tasks:"任务进度"};
const statusName = {succeeded:"已完成", completed:"已完成", failed:"失败", partially_succeeded:"部分完成", budget_stopped:"额度用尽", cancelled:"已取消", running:"正在执行", queued:"等待执行", pending:"待执行", interrupted:"已中断", blocked:"不可用", attempted:"调用中"};
const workflowName = {chat:"学习问答", jd_extract:"岗位要求提取", knowledge_build:"知识构建", project_discovery:"项目调研", learning_plan:"学习计划", project_analysis:"项目分析"};
const state = {view:"chat", data:null, cid:null, conversation:null, selected:new Set(), filter:"all", search:"", draft:"", useModel:true, agent:"auto", sending:false, polling:false, goals:null, runs:[], dialogPub:null, requestId:null, source:null};
let pollTimer, toastTimer;
const requests = new Map();

function hydrate(root = document) { root.querySelectorAll("[data-icon]").forEach(node => { node.innerHTML = icon(node.dataset.icon); }); }
async function api(path, options = {}) {
  const response = await fetch("/desk" + path, {...options, headers:{"X-CareerForge-Desk":"1", "Content-Type":"application/json", ...options.headers}});
  const body = await response.json();
  if (!response.ok) {
    let detail = body.detail ?? body.error ?? "请求失败";
    if (typeof detail === "object") detail = detail.message ?? JSON.stringify(detail);
    throw new Error(String(detail));
  }
  return body;
}
const post = (path, body) => api(path, {method:"POST", body:JSON.stringify(body)});
function toast(text) { clearTimeout(toastTimer); $("#toast").textContent = text; $("#toast").hidden = false; toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4800); }
function error(text) { const box = $("#global-error"); box.textContent = text; box.hidden = false; }
function clearError() { $("#global-error").hidden = true; }
function badge(status) { return `<span class="badge ${["completed","succeeded"].includes(status)?"ok":["failed","cancelled","interrupted","blocked"].includes(status)?"fail":["running","queued"].includes(status)?"running":""}">${esc(statusName[status] || status)}</span>`; }
function heading(eyebrow, title, description, action = "") { return `<div class="page-header"><div><div class="eyebrow">${eyebrow}</div><h2>${title}</h2><p class="page-subtitle">${description}</p></div>${action}</div>`; }
function empty(title, description, action = "", name = "book") { return `<div class="empty"><div class="empty-icon">${icon(name)}</div><h3>${title}</h3><p>${description}</p>${action}</div>`; }
function pubById(id) { return state.data?.publications.find(p => p.publication_id === id); }
function planById(id) { return state.data?.plans.find(p => p.plan_id === id); }
function activeJobs() { return state.data?.jobs.filter(j => j.status === "running") || []; }
function preserveDraft() { const input = $("#question"); if (input) state.draft = input.value; }

async function loadOverview() {
  state.data = await api("/overview");
  $("#knowledge-count").textContent = state.data.publications.length;
  $("#model-name").textContent = state.data.model_configured ? state.data.model : "模型未配置";
  $("#connection-label").textContent = "本地服务已连接";
  $("#connection-dot").classList.remove("offline");
  renderHistory();
}
function renderHistory() {
  $("#history").innerHTML = state.data.conversations.slice(0,12).map(c => `<button data-conversation="${esc(c.id)}" class="${c.id===state.cid?"selected":""}" title="${esc(c.title)}">${esc(c.title)}</button>`).join("") || '<p class="history-empty">从第一个问题开始。</p>';
}
async function navigate(view) {
  if (!titles[view]) view = "chat";
  preserveDraft(); clearError(); state.view = view;
  history.replaceState(null, "", "#" + view);
  $("#page-name").textContent = titles[view];
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $("#sidebar").classList.remove("open");
  render();
  try {
    if (view === "chat" && state.cid) {
      const cid = state.cid, conversation = await api("/conversations/" + cid);
      if (state.view === view && state.cid === cid) { state.conversation = conversation; reconcileSource(); preserveDraft(); render(); }
    }
    if (view === "goals") { state.goals = await api("/goals"); if (state.view === view) render(); }
    if (view === "tasks") { state.runs = await api("/runs"); if (state.view === view) render(); }
  } catch (e) { error(e.message); }
}
function render() {
  if (!state.data) return;
  $("#main-content").innerHTML = ({chat:renderChat, knowledge:renderKnowledge, plans:renderPlans, goals:renderGoals, tasks:renderTasks}[state.view])();
  hydrate($("#main-content"));
}

function rail() {
  const pubs = state.data.publications, plans = state.data.plans;
  const active = plans.find(p => p.active_version_id);
  const version = active?.versions.find(v => v.version_id === active.active_version_id);
  const projects = new Set(pubs.map(p => p.repository)).size;
  return `<aside class="chat-rail"><div class="rail-heading">${icon("target")} 我的学习方向</div>
    <div class="rail-card"><div class="overline">YOUR NEXT CHAPTER</div><h3>${esc(version?.title || "从真实项目，理解 AI 工程")}</h3><p>${version ? `已启用 ${version.modules.length} 个学习模块，随时继续。` : "把岗位要求拆成具体知识，再用源码、练习和反馈逐步掌握。"}</p><div class="stats"><div><b>${pubs.length}</b><small>已发布知识</small></div><div><b>${projects}</b><small>关联项目</small></div></div><button data-action="go-plans">${version ? "查看我的学习计划" : "创建我的第一份计划"} ↗</button></div>
    <div class="rail-heading">${icon("book")} 可以开始的材料</div>${pubs.slice(0,3).map((p,i) => `<div class="resource-item"><div class="resource-icon">${String(i+1).padStart(2,"0")}</div><div class="resource-copy"><button data-action="publication" data-id="${p.publication_id}">${esc(p.title)}</button><small>${esc(p.repository)}</small><span class="mini-tag">${p.unit_type === "architecture"?"架构理解":"实现细节"}</span></div></div>`).join("")}
    <button class="text-button" data-view="knowledge">浏览全部知识 ${icon("arrow")}</button><div class="rail-bottom">${icon("shield")}<span>讲解保留证据边界。阅读过材料，并不意味着已经掌握。</span></div></aside>`;
}
function welcome() {
  return `<section class="welcome"><span class="welcome-pill">${icon("spark")} 你的项目学习伙伴</span><h1>把岗位要求，<br>变成<em>真正掌握的能力。</em></h1><p class="welcome-intro">从一个问题开始，在真实项目中理解技术。<br>一起读源码、拆解设计，让每一步学习都有迹可循。</p>
    <div class="learning-path"><span><b>1</b>明确目标</span><i></i><span class="path-current"><b>2</b>探索项目</span><i></i><span><b>3</b>深入学习</span><i></i><span><b>4</b>练习与反馈</span></div>
    <div class="section-heading"><h3>今天，想从哪里开始？</h3><small>选择一个方向，或直接提问</small></div><div class="prompt-grid">
    <button class="prompt-card" data-view="goals"><span class="prompt-icon">${icon("target")}</span><span class="card-arrow">↗</span><strong>从目标岗位出发</strong><p>导入你的 JD，梳理岗位需要的技术与能力。</p></button>
    <button class="prompt-card" data-view="knowledge"><span class="prompt-icon green">${icon("layers")}</span><span class="card-arrow">↗</span><strong>探索真实项目</strong><p>浏览已审核的架构、Python 实现与源码证据。</p></button>
    <button class="prompt-card" data-action="suggest" data-question="请解释RRF排名融合和候选裁剪各自的作用"><span class="prompt-icon orange">${icon("code")}</span><span class="card-arrow">↗</span><strong>讲透一个技术问题</strong><p>RRF 是怎样融合检索结果的？从代码里找答案。</p></button>
    <button class="prompt-card" data-view="plans"><span class="prompt-icon blue">${icon("route")}</span><span class="card-arrow">↗</span><strong>规划下一步学习</strong><p>选择学习材料，安排模块，再动手验证理解。</p></button></div>
    <div class="welcome-foot">${icon("shield")} 已连接 ${state.data.publications.length} 个已发布知识单元 · 回答可追溯到源码</div></section>`;
}
function blockList(turn) {
  const blocks = new Map();
  for (const event of turn.events || []) {
    const p = event.payload || {};
    if (event.type === "answer.block.validated") blocks.set(p.block_id, {...p.content, block_id:p.block_id});
    if (["answer.evidence_invalidated", "answer.block.retracted"].includes(event.type)) blocks.delete(p.block_id);
  }
  return [...blocks.values()];
}
function citationButton(c, turnIndex, blockIndex, index) {
  return `<button class="citation" data-action="citation" data-turn="${turnIndex}" data-block="${blockIndex}" data-index="${index}" title="查看源码证据">${icon("code")} ${esc(c.path?.split("/").pop() || "源码")} : ${esc(c.start_line)}–${esc(c.end_line)}</button>`;
}
const intentNames={chat:"日常交流",progress:"查询进度",start:"开始学习",explain:"源码讲解",reexplain:"换个方式讲解",practice:"创建练习",assessment:"评估答案",plan:"生成计划",discover:"调研项目",change_goal:"调整目标",confirm_plan:"确认计划",submit_answer:"提交答案",advance:"推进模块",pause:"暂停",resume:"恢复",cancel:"取消",unknown:"需要澄清"};
function turnsHTML() {
  const turns = state.conversation?.turns || [];
  const rendered = turns.map((turn, ti) => {
    const blocks = blockList(turn), job = state.data.jobs.find(j => j.turn_id === turn.id);
    const interrupted = job?.status === "interrupted";
    return {time:turn.events?.[0]?.occurred_at || "", html:`<article class="turn"><div class="user-message"><p>${esc(turn.question)}</p><span class="avatar">我</span></div><div class="assistant-label"><img src="/assets/mark.svg" alt=""><span>教学 Agent</span><span>基于已审核知识</span></div>
      ${blocks.map((b,bi) => `<div class="answer-block ${b.kind === "evidence_gap"?"gap":""}">${b.kind === "repository_fact" ? '<div class="answer-label">✓ 已审核事实 · 可追溯源码</div>':""}<p>${esc(b.text || b.question || b.message || "")}</p>${b.citations?.length ? `<div class="citations">${b.citations.map((c,ci) => citationButton(c,ti,bi,ci)).join("")}</div>`:""}</div>`).join("")}
      ${["queued","running"].includes(turn.status) && !interrupted ? '<div class="pending"><span class="spinner"></span>正在检索材料、生成讲解并核对依据…</div>' : ""}
      ${["failed","blocked","cancelled","interrupted"].includes(turn.status) || interrupted ? `<div class="inline-error">${interrupted?"上次执行已中断，未自动重复模型调用。":"本次问题未能完成："+esc(turn.events?.find(e=>e.type==="answer.interrupted")?.payload?.code||turn.status)+"。可修改问题重试，或查看任务进度。"}</div>`:""}
      ${turn.status === "completed" && !blocks.length ? '<p class="muted">该轮没有可显示的回答内容。</p>':""}</article>`};
  });
  for(const action of state.conversation?.actions||[])rendered.push({time:action.created_at||"",html:`<article class="turn flow-receipt"><div class="user-message"><p>${esc(action.question)}</p><span class="avatar">我</span></div><div class="answer-label">${["dialogue","intent"].includes(action.agent)?"对话协调 Agent":"学习协调 · 流程回执"}</div>${action.routing?`<p class="form-help">理解为：${(action.routing.intents||[]).map(i=>esc(intentNames[i]||i)).join(" → ")}</p>`:""}${action.renewal_notice?`<p class="form-help">${esc(action.renewal_notice)}</p>`:""}<p>${esc(action.reply)}${action.status==="running"?" · 正在运行":""}</p>${(action.action_receipts||[]).filter(x=>x.status==="failed").map(x=>`<div class="inline-error">${esc(x.failure_code)}</div>`).join("")}</article>`});
  return rendered.sort((a,b)=>a.time.localeCompare(b.time)).map(x=>x.html).join("")+flowHTML();
}
async function flowCommand(text, extra={}) {
  if(state.sending)return;
  if(state.draft.trim())return toast("输入框里有尚未发送的内容，请先发送或清空后操作。");
  state.draft=text;render();
  await sendQuestion({preventDefault(){}},extra);
}
function flowHTML() {
  const f=state.conversation?.flow||{}, v=f.status||{}, plan=f.plan, version=plan?.versions.at(-1), busy=activeJobs().length>0 || (v.active_stages||[]).length>0;
  const command=(label,text,versionId="")=>`<button class="secondary" data-action="flow-command" data-command="${esc(text)}" data-version="${esc(versionId)}" ${busy && text!=="查询学习进度"?"disabled":""}>${esc(label)}</button>`;
  const stages={awaiting_jd:"等待学习目标",awaiting_extraction:"等待提取岗位要求",extracting:"正在提取要求",awaiting_language_choice:"选择实现语言",awaiting_goal_confirmation:"确认目标与材料",awaiting_project_selection:"选择调研项目",building:"分析与独立审核",planning:"生成计划",awaiting_plan_acceptance:"确认学习计划",tutoring:"模块学习",practicing:"等待提交答案",practice_submitted:"答案已提交",assessing:"正在评估答案",feedback:"反馈与复习",learning_completed:"本计划阅读流程已完成"};
  let html=`<section class="flow-card"><div class="eyebrow">LEARNING JOURNEY</div><h3>${esc(stages[v.milestone]||"把对话变成学习行动")}</h3><p class="form-help">目标 → 已审核材料 → 计划确认 → 源码讲解 → 练习 → 反馈 → 下一模块</p>`;
  const liveJobs=activeJobs().filter(j=>j.conversation_id===state.cid);
  if(liveJobs.length)html+=`<div class="pending" role="status" aria-live="polite">${liveJobs.map(j=>j.agent==="intent"?"正在理解意图与上下文":j.agent==="dialogue"?"对话协调 Agent 正在回复":"教学 / 学习工作流正在后台执行").join("；")}。每 2.5 秒刷新，可继续输入问题。</div>`;
  if(f.error)html+=`<p class="inline-error">${esc(f.error.message)}</p>`;
  if(v.last_error_code)html+=`<p class="inline-error">${v.execution_state==="succeeded"?"历史任务曾失败（当前流程已继续）":"最近任务未完成"}：${esc(v.last_error_code)}。已完成的材料和答案会保留。</p>`;
  if(v.workflow_id)html+=`<p class="form-help">已发布 ${v.publication_count||0} 份材料 · 已用 ${v.calls_consumed||0} 次模型调用 · ${esc(v.control_state)}</p>`;
  if(v.active_stages?.length)html+=v.active_stages.map(x=>`<p class="pending">${esc(x.label)} · ${esc(statusName[x.status]||x.status)}</p>`).join("");
  if(v.source_jd_revision_id && !v.goal_version_id)html+=command("提取岗位要求","确认提取");
  if(f.goal)html+=`<details open><summary>已提取的岗位要求</summary><div class="requirements">${requirementItems(f.goal.requirements).map(r=>`<span class="tag">${esc(r.technology)} · ${esc(r.depth||"深度未指定")}</span>`).join("")}</div></details>`;
  if(v.language_question)html+=`<p>${esc(v.language_question)}</p>${command("选择 Python","实现语言选择 Python")}`;
  if(v.goal_version_id && !version)html+=`<div class="flow-actions">${command("调研项目（最多 32 次调用）","搜项目并入库")}${command("确认目标并构建知识（最多 32 次）","确认目标")}</div>`;
  if(f.projects?.length)html+=`<details open><summary>调研候选项目 · 选择固定源码继续分析</summary>${f.projects.map(p=>`<div class="resource-item"><span>${esc(p.repository)}<small>${esc(p.commit||p.commit_sha||"")}</small></span><button class="secondary" data-action="flow-build-source" data-source="${esc(p.source_id)}" ${busy?"disabled":""}>分析并审核知识（最多 32 次）</button></div>`).join("")}</details>`;
  if(!version)html+=`<details class="flow-materials"><summary>从已有的 ${state.data.publications.length} 份审核材料开始</summary><p class="form-help">选择与目标相关的材料即可生成计划；材料未覆盖的岗位要求仍需补充调研。</p><div class="flow-material-list">${state.data.publications.map(p=>`<label><input type="checkbox" data-select="${p.publication_id}" ${state.selected.has(p.publication_id)?"checked":""}><span>${esc(p.title)}<small>${esc(p.repository)}</small></span></label>`).join("")}</div><button class="primary" data-action="flow-plan" ${busy?"disabled":""}>按所选材料生成计划</button></details>`;
  if(version){html+=`<div class="flow-plan"><h4>${esc(version.title)}</h4><span class="tag">${version.accepted?"已启用":"待确认"} · 第 ${version.version} 版</span><ol>${version.modules.map(m=>`<li>${esc(m.content.title||"材料暂不可用")}</li>`).join("")}</ol>${version.accepted?command("开始当前模块讲解","开始讲解"):command("确认此版本并开始学习","确认学习计划",version.version_id)}</div>`;}
  if(version?.accepted && v.milestone!=="learning_completed")html+=`<div class="flow-actions">${command("开始练习","开始练习")}${command("我还没理解","我没理解")}</div>`;
  const spec=f.activity?.specification;
  if(spec){html+=`<div class="flow-exercise"><h4>当前模块练习</h4><p>${esc(spec.prompt||spec.question||"")}</p><ul>${(spec.rubric||[]).map(r=>`<li>${esc(r.criterion)}</li>`).join("")}</ul><p class="form-help">在输入框中输入「提交答案：你的解释」。答案将保存在当前练习中。</p>${f.attempt?.state==="submitted"?`<p class="tag green">答案已保存</p>${command("评估我的答案","评估我的答案")}`:`<button class="secondary" data-action="flow-answer">填写答案</button>`}</div>`;}
  if(f.assessment){const r=f.assessment.result;html+=`<div class="flow-assessment"><h4>${f.assessment.historical?"最近一次练习反馈（历史）":"练习反馈"} · ${esc(statusName[f.assessment.status]||f.assessment.status)}</h4>${r?(r.rubric_item_decisions||[]).map(x=>`<p><span class="tag">${esc({demonstrated:"已体现",not_yet:"待补充",unknown:"尚不确定"}[x.outcome])}</span> ${esc(x.explanation)}</p>`).join(""):'<p>等待评估结果；失败时可以重新请求评估。</p>'}<p class="form-help">模型评估是可复核的建议，不是独立人工认证，也不会自动标记掌握。</p></div>`;}
  if(f.teaching?.can_acknowledge_module){const incomplete=f.teaching.delivery.requires_incomplete_acceptance;html+=`<div class="flow-actions">${command(incomplete?"接受材料缺口，确认阅读完成并进入下一模块":"确认阅读完成，进入下一模块",incomplete?"接受材料缺口并确认阅读完成，进入下一模块":"确认阅读完成，进入下一模块")}</div>`;}
  if(f.module_ack)html+=`<p class="tag green">已记录模块阅读完成 · ${f.module_ack.next_active_module_id?"可继续下一模块":"本计划阅读流程已完成"}</p>`;
  html+=`<div class="flow-actions">${command("查询进度","查询学习进度")}</div></section>`;
  return html;
}
function renderChat() {
  const busy = state.sending;
  return `<div class="chat-layout ${state.source?"with-source":""}"><section class="chat-center">${state.cid ? `<section class="conversation"><div class="conversation-top"><h2>${esc(state.conversation?.actions?.[0]?.question || state.conversation?.turns[0]?.question || "学习对话")}</h2><button class="text-button" data-action="new-chat">新对话 ＋</button></div><div id="turns">${state.conversation ? turnsHTML() : '<div class="loading-page"><span class="spinner"></span>正在读取会话</div>'}</div></section>`:welcome()+flowHTML()}
    <div class="composer-area"><form id="chat-form" class="composer"><label for="question" class="sr-only" hidden>学习问题</label><textarea id="question" maxlength="10000" rows="2" aria-label="输入学习问题" placeholder="说“开始学习”，或随时问我进度；教学运行时也可以继续聊天">${esc(state.draft)}</textarea><div class="composer-toolbar"><select id="agent-mode" aria-label="对话对象"><option value="auto" ${state.agent==="auto"?"selected":""}>自动分工</option><option value="dialogue" ${state.agent==="dialogue"?"selected":""}>对话协调 Agent</option><option value="tutor" ${state.agent==="tutor"?"selected":""}>源码教学 Agent</option></select><label class="mode-control"><input id="use-model" type="checkbox" ${state.useModel?"checked":""} ${state.data.model_configured?"":"disabled"}>${icon("spark")}启用 AI <span>· 学习含按需补充最多 40 次 · 手动对话 1 次</span></label><button class="send" type="submit" aria-label="发送学习问题" ${busy?"disabled":""}>${busy?'<span class="spinner"></span>':icon("send")}</button></div></form><div class="composer-hint">Enter 发送 · Shift + Enter 换行　｜　协调 Agent 负责交流和进度，教学 Agent 负责源码与练习。后台运行时可继续聊天。</div></div></section>${state.source?sourcePanel():rail()}</div>`;
}
function renderKnowledge() {
  const all = state.data.publications;
  const filtered = all.filter(p => (state.filter === "all" || (state.filter === "architecture" ? p.unit_type === "architecture" : p.unit_type !== "architecture")) && `${p.title} ${p.repository} ${p.topic_links.map(t=>t.topic_key).join(" ")}`.toLowerCase().includes(state.search.toLowerCase()));
  return `<div class="page">${heading("EVIDENCE LIBRARY", "把知识放回真实项目里", "已发布、已审核的学习材料，每条事实都能回到固定版本的源码。", `<span class="count-note">${all.length} 个知识单元 · ${new Set(all.map(p=>p.repository)).size} 个项目</span>`)}
    <div class="toolbar"><div class="search">${icon("search")}<input id="knowledge-search" aria-label="搜索知识或项目" placeholder="搜索知识、项目或技术主题…" value="${esc(state.search)}"></div><div class="filter-tabs"><button data-filter="all" class="${state.filter==="all"?"active":""}">全部材料</button><button data-filter="architecture" class="${state.filter==="architecture"?"active":""}">架构理解</button><button data-filter="implementation" class="${state.filter==="implementation"?"active":""}">实现细节</button></div></div>
    <div class="knowledge-grid">${filtered.map(p=>`<article class="knowledge-card"><div class="knowledge-card-top"><span class="prompt-icon ${p.unit_type==="architecture"?"":"green"}">${icon(p.unit_type==="architecture"?"layers":"code")}</span><label><input type="checkbox" data-select="${p.publication_id}" ${state.selected.has(p.publication_id)?"checked":""}>加入计划</label></div><div><div class="repo">${esc(p.repository)}</div><h3>${esc(p.title)}</h3></div><p>${esc(p.facts[0]?.text || "打开查看已审核材料")}</p><div class="card-tags"><span class="tag green">已发布</span><span class="tag">${p.unit_type==="architecture"?"架构理解":"实现细节"}</span><span class="tag">${p.citations.length} 处源码引用</span></div><div class="card-actions"><button class="text-button" data-action="publication" data-id="${p.publication_id}">阅读材料 ${icon("arrow")}</button><button class="secondary" data-action="ask-publication" data-id="${p.publication_id}">请教助手</button></div></article>`).join("")}</div>
    ${!filtered.length?empty("没有找到匹配材料","试试项目名称或更简短的技术关键词。","","search"):""}
    ${state.selected.size?`<div class="selection-bar"><span>已选择 <strong>${state.selected.size}</strong> 个知识单元</span><button class="primary" data-action="propose-plan">生成学习计划 ${icon("arrow")}</button></div>`:""}</div>`;
}
function renderPlans() {
  return `<div class="page">${heading("YOUR LEARNING PATH", "让学习有一条清晰的路线", "从已审核材料生成模块，确认后开始。练习记录会保留你的思考过程。", '<button class="primary" data-view="knowledge">＋ 选择材料创建计划</button>')}
    ${state.data.plans.length ? state.data.plans.map(p => {const v=p.versions.at(-1);return `<article class="plan-card"><div class="plan-head"><div><span class="tag ${v.accepted?"green":"amber"}">${v.accepted?"学习中":"草稿 · 待你确认"}</span><h3>${esc(v.title)}</h3><small>${v.modules.length} 个模块 · 第 ${v.version} 版</small></div>${v.accepted?'<span class="badge ok">已启用</span>':`<button class="primary" data-action="accept-plan" data-id="${p.plan_id}">确认并开始学习</button>`}</div><div class="plan-modules">${v.modules.map((m,i)=>`<section class="module"><span class="module-number">${String(i+1).padStart(2,"0")}</span><div class="module-body"><h4>${esc(m.content.title || "材料当前不可用")}</h4>${(m.content.steps || []).map(s=>`<p>· ${esc(typeof s==="string"?s:s.title||s.description||"学习步骤")}</p>`).join("")}<div class="module-actions">${m.available?`<button class="text-button" data-action="publication" data-id="${m.content.publication_id}">阅读源码材料</button><button class="text-button" data-action="ask-module" data-plan="${p.plan_id}" data-id="${m.module_id}">向助手提问</button>${v.accepted?`<button class="secondary" data-action="practice" data-plan="${p.plan_id}" data-id="${m.module_id}">记录练习与反思</button>`:""}`:'<span class="tag amber">依赖证据不可用</span>'}${state.data.observations.some(o=>o.module_id===m.module_id)?'<span class="tag green">已有练习记录</span>':""}</div></div></section>`).join("")}</div><div class="plan-note">计划根据所选知识编排；尚未验证的能力保持未知。提问使用普通学习问答，不自动完成模块。</div></article>`;}).join("") : empty("你的第一份学习计划，从这里开始", "选出想学习的架构或实现材料，系统会按顺序生成可确认的学习模块。现有交付尚未创建学习计划。", '<button class="primary" data-view="knowledge">选择学习材料 '+icon("arrow")+'</button>', "route")}</div>`;
}
function requirementItems(value) {
  if (Array.isArray(value)) return value.flatMap(requirementItems);
  if (!value || typeof value!=="object") return [];
  if (value.technology) return [value];
  return Object.values(value).flatMap(requirementItems);
}
function renderGoals() {
  return `<div class="page">${heading("START WITH YOUR GOAL", "先明确，你想成为什么样的人", "导入岗位描述，保留原文与版本；再从真实要求出发安排学习。")}
    <div class="goal-grid"><section class="panel"><h3>添加一个目标岗位</h3><form id="goal-form"><label class="form-label" for="job-title">岗位名称</label><input class="input" id="job-title" required maxlength="200" placeholder="例如：AI Agent 应用工程师"><label class="form-label" for="job-text">岗位描述 / JD 原文</label><textarea class="textarea" id="job-text" required maxlength="50000" placeholder="粘贴岗位职责、技术要求和任职条件…"></textarea><p class="form-help">保存原文不调用模型。保存后可单独选择「提取岗位要求」，每次最多 3 次模型调用。</p><button class="primary" type="submit">保存岗位目标 ${icon("arrow")}</button></form></section><section class="panel"><h3>已保存的岗位目标</h3>${state.goals ? state.goals.documents.map(d=>`<div class="goal-document"><span class="tag">原文已保存</span><h3>${esc(d.job_title||"岗位描述")}</h3><div class="jd-original">${esc(d.raw_text)}</div><button class="secondary" data-action="extract" data-id="${d.jd_revision_id}">提取岗位要求</button></div>`).join("") || '<p class="form-help">还没有导入岗位原文。填写左侧表单开始。</p>' : '<div class="loading-page"><span class="spinner"></span>读取岗位目标</div>'}</section></div>
    ${state.goals?.goals.length?`<section class="panel goal-requirements"><h3>已提取的要求</h3><p class="form-help">这些是岗位要求，不代表你已经掌握。</p>${state.goals.goals.map(g=>`<h4>${esc(g.title)}</h4><div class="requirements">${requirementItems(g.requirements).map(r=>`<span class="tag" title="${esc(r.quote||"")}">${esc(r.technology)}${r.depth?" · "+esc(r.depth):""}</span>`).join("")}</div>`).join("")}</section>`:""}</div>`;
}
function renderTasks() {
  const sorted = [...state.runs].sort((a,b)=>Number(b.from_desk)-Number(a.from_desk));
  return `<div class="page">${heading("EVERY STEP IS VISIBLE", "学习的每一步，都有进度", "查看本页提交的模型任务和已有运行记录。任务完成与回答完整性分别判断。",'<button class="secondary" data-action="refresh-tasks">'+icon("refresh")+' 刷新状态</button>')}
    ${sorted.length?`<div class="table-wrap"><table><thead><tr><th>学习任务</th><th>来源</th><th>执行状态</th><th>模型调用</th><th></th></tr></thead><tbody>${sorted.map(r=>`<tr><td><div class="task-title">${esc(workflowName[r.workflow]||r.workflow)}</div><div class="table-id">${esc(r.id.slice(0,16))}</div></td><td>${r.from_desk?"本工作台":"已有记录"}</td><td>${badge(r.desk_status==="interrupted"?"interrupted":r.status)}</td><td>${r.calls} 次<small>${esc([...new Set(r.roles.map(x=>x.role))].join(" · "))}</small></td><td><button class="text-button" data-action="task-detail" data-id="${r.id}">查看详情</button></td></tr>`).join("")}</tbody></table></div>`:empty("还没有运行记录","发送一个学习问题，或者提取岗位要求后，可以在这里查看执行状态。","","activity")}</div>`;
}
function openDialog(title, body, subtitle = "") {
  $("#dialog-content").innerHTML=`<div class="dialog-head"><div><h2>${title}</h2>${subtitle?`<p class="source-meta">${subtitle}</p>`:""}</div><button class="icon-button" data-action="close-dialog" aria-label="关闭详情">${icon("close")}</button></div><div class="dialog-body">${body}</div>`;
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
}
function sourcePanel() {
  const selected=state.source, t=selected.target;
  const header=`<div class="source-heading"><div><div class="eyebrow">SOURCE EVIDENCE</div><h3 id="source-title">知识来源 · 项目源码</h3></div><button class="icon-button" data-action="close-source" aria-label="关闭源码面板">${icon("close")}</button></div>`;
  let body='<div class="pending"><span class="spinner"></span>正在校验并读取固定版本源码…</div>';
  if(selected.error)body=`<div class="inline-error" role="alert">源码暂不可用：${esc(selected.error)}</div>`;
  if(t){
    const lines=t.display.text.replace(/\r\n/g,"\n").replace(/\n$/,"").split("\n");
    const valid=/^[a-f0-9]{40}$/i.test(t.commit)&&/^[\w.-]+\/[\w.-]+$/.test(t.repository);
    const url=valid?`https://github.com/${t.repository}/blob/${t.commit}/${t.path.split("/").map(encodeURIComponent).join("/")}#L${t.start_line}-L${t.end_line}`:null;
    body=`<span class="tag green">固定快照 · 引用已校验</span><h4 class="source-repo">${esc(t.repository)}</h4><p class="source-path">${esc(t.path)}</p><dl class="source-details"><dt>版本 / Commit</dt><dd>${esc(t.commit)}</dd><dt>引用范围</dt><dd>L${t.start_line}–L${t.end_line}</dd></dl><div class="source-explanation">${esc(selected.explanation)}</div><div class="source-code" tabindex="0" aria-label="引用源码，第 ${t.start_line} 至 ${t.end_line} 行"><pre>${lines.map((line,i)=>`<span class="source-line"><span class="line-number" aria-hidden="true">${t.start_line+i}</span><code>${esc(line)||" "}</code></span>`).join("")}</pre></div><p class="form-help">高亮区域为该条回答引用的完整代码片段，来自固定版本快照。</p>${url?`<a class="text-button" href="${esc(url)}" target="_blank" rel="noopener noreferrer">在 GitHub 查看此版本 ↗</a>`:""}<details class="source-hashes"><summary>查看来源校验信息</summary><p>Source ID: ${esc(t.source_id)}</p><p>文件 SHA-256: ${esc(t.display.file_sha256)}</p><p>片段 SHA-256: ${esc(t.display.text_sha256)}</p></details>`;
  }
  return `<aside class="source-panel" aria-labelledby="source-title" aria-live="polite">${header}${body}</aside>`;
}
async function openSource(button) {
  const block=blockList(state.conversation.turns[Number(button.dataset.turn)])[Number(button.dataset.block)];
  if(!block)return;
  const selected={blockId:block.block_id,index:Number(button.dataset.index),explanation:block.text||"",target:null,error:null};
  preserveDraft();state.source=selected;render();
  try{
    const target=await api(`/navigation/${encodeURIComponent(selected.blockId)}?citation_index=${selected.index}`);
    if(state.source!==selected)return;
    if(!target.server_location_valid || !target.display?.content_complete)throw new Error("源码定位未通过校验");
    selected.target=target;
  }catch(e){if(state.source!==selected)return;selected.error=e.message;}
  if(state.view==="chat"){preserveDraft();render();$(".source-code")?.focus({preventScroll:true});}
}
function reconcileSource() {
  if(state.source && !state.conversation?.turns.some(t=>blockList(t).some(b=>b.block_id===state.source.blockId))){
    state.source=null;preserveDraft();render();
  }
}
function sourceHTML(c) {
  const valid = /^[a-f0-9]{40}$/i.test(c.commit||"") && /^[\w.-]+\/[\w.-]+$/.test(c.repository||"");
  const url=valid?`https://github.com/${c.repository}/blob/${c.commit}/${String(c.path).split("/").map(encodeURIComponent).join("/")}#L${Number(c.start_line)}`:null;
  return `<div class="source-meta">${esc(c.repository)} · ${esc(c.commit?.slice(0,12))}<br>${esc(c.path)} · L${esc(c.start_line)}–L${esc(c.end_line)}</div><pre class="code"><code>${esc(c.quote)}</code></pre>${url?`<a class="text-button" href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开固定版本源码 ↗</a>`:""}`;
}
function openPublication(id) {
  const p=pubById(id); if(!p){toast("该材料当前不可用，请刷新知识库。");return;}
  state.dialogPub=id;
  openDialog(esc(p.title), `<div class="card-tags"><span class="tag green">已发布</span><span class="tag">${esc(p.unit_type==="architecture"?"架构理解":"实现细节")}</span></div><h3>已审核的核心事实</h3>${p.facts.map(f=>`<section class="fact"><p>${esc(f.text)}</p>${f.conditions?.length?`<div class="condition">${f.conditions.map(esc).join("<br>")}</div>`:""}</section>`).join("")}<h3>源码依据 · ${p.citations.length} 处</h3>${p.citations.map(sourceHTML).join("")}<h3>证据边界</h3>${p.limitations.map(l=>`<p>${esc(l)}</p>`).join("")}<div class="dialog-actions"><button class="secondary" data-action="select-dialog" data-id="${id}">${state.selected.has(id)?"已加入计划选择":"加入计划选择"}</button><button class="primary" data-action="ask-publication" data-id="${id}">围绕材料提问</button></div>`, `${esc(p.repository)} · ${esc(p.commit.slice(0,12))}`);
}
function moduleFor(planId,moduleId) { return planById(planId)?.versions.at(-1)?.modules.find(m=>m.module_id===moduleId); }
async function suggest(question) { $("#detail-dialog").close(); state.draft=question; await navigate("chat"); $("#question").focus(); }
async function newChat() { state.source=null;state.selected.clear();state.cid=null;state.conversation=null;state.draft="";state.requestId=null;await navigate("chat");renderHistory();$("#question").focus(); }
async function openConversation(id) {
  preserveDraft();state.source=null;state.cid=id;state.conversation=null;await navigate("chat");
  try{const result=await api("/conversations/"+id);if(state.cid===id){state.conversation=result;render();renderHistory();}}catch(e){error(e.message);}
}
async function sendQuestion(event, extra = {}) {
  event.preventDefault();
  if(state.sending) return;
  const text=$("#question").value.trim();if(!text)return;
  state.draft=text;state.useModel=$("#use-model").checked;
  state.sending=true;clearError();$(".send").disabled=true;
  // Retain the same ID after a network error so a retry cannot duplicate spend.
  const signature=JSON.stringify([state.cid,text,state.useModel,state.agent,extra]);
  if(!requests.has(signature))requests.set(signature,crypto.randomUUID());
  try{
    const job=await post("/messages",{question:text,conversation_id:state.cid,request_id:requests.get(signature),use_model:state.useModel,agent:state.agent,...extra});
    requests.delete(signature);
    state.cid=job.conversation_id;state.draft="";
    await loadOverview();state.conversation=await api("/conversations/"+state.cid);
    toast(job.status==="running"?"已提交，正在执行学习任务。":"学习动作已处理，请查看对话回执。");
  }catch(e){error(e.message);}finally{state.sending=false;render();if(state.cid)$(".composer-area")?.scrollIntoView({block:"end",behavior:"smooth"});}
}
async function poll() {
  if(state.polling || document.hidden || !state.data)return;
  state.polling=true;
  try{
    if(activeJobs().length || state.conversation?.turns.some(t=>["queued","running"].includes(t.status))){
      await loadOverview();
      if(state.cid && state.view==="chat"){
        const cid=state.cid, result=await api("/conversations/"+cid);
        if(state.cid!==cid || state.view!=="chat")return;
        state.conversation=result;reconcileSource();
        if($("#turns"))$("#turns").innerHTML=turnsHTML();
        const send=$(".send");if(send){send.disabled=state.sending;send.innerHTML=send.disabled?'<span class="spinner"></span>':icon("send");}
      }
      if(state.view==="tasks"){state.runs=await api("/runs");render();}
      if(state.view==="goals" && !activeJobs().length){state.goals=await api("/goals");render();}
    }
  }catch(e){error("读取进度失败："+e.message);}finally{state.polling=false;}
}
async function busy(button, fn) {
  if(button.disabled)return;button.disabled=true;
  try{await fn();}catch(e){toast(e.message);error(e.message);}finally{button.disabled=false;}
}
async function handleAction(button) {
  const a=button.dataset.action,id=button.dataset.id;
  if(a==="close-dialog")return $("#detail-dialog").close();
  if(a==="flow-command")return flowCommand(button.dataset.command,{confirm_version:button.dataset.version||null});
  if(a==="flow-build-source")return flowCommand("确认目标",{source_ids:[button.dataset.source]});
  if(a==="flow-plan"){
    if(!state.selected.size)return toast("先勾选要学习的材料。");
    return flowCommand("使用所选已审核材料创建学习计划",{publication_ids:[...state.selected]});
  }
  if(a==="flow-answer")return suggest("提交答案：");
  if(a==="new-chat")return newChat();
  if(a==="go-plans")return navigate("plans");
  if(a==="publication")return openPublication(id);
  if(a==="suggest")return suggest(button.dataset.question);
  if(a==="ask-publication") {const p=pubById(id);return suggest(`请根据已发布知识讲解「${p.title}」的关键条件和实现边界。`);}
  if(a==="ask-module") {const m=moduleFor(button.dataset.plan,id);return suggest(`请根据已发布知识讲解「${m.content.title}」，帮助我理解实现条件与错误边界。`);}
  if(a==="select-dialog"){state.selected.add(id);button.textContent="已加入计划选择";toast("材料已选中，可在知识库生成计划。");return;}
  if(a==="citation")return openSource(button);
  if(a==="close-source"){preserveDraft();state.source=null;render();return;}
  if(a==="propose-plan")return openDialog("创建一份学习计划",`<p>将所选的 ${state.selected.size} 个知识单元编排成学习模块。先生成草稿，确认后才会启用。</p><form id="plan-form"><label class="form-label" for="plan-title">计划名称</label><input class="input" id="plan-title" required maxlength="200" value="我的 AI 工程学习计划"><label class="form-label" for="plan-intent">学习方向</label><select class="select" id="plan-intent"><option value="learn_project">理解项目与源码</option><option value="technical_design">学习技术设计</option><option value="function">掌握具体功能</option><option value="build_project">为构建项目做准备</option></select><p class="form-help">根据已审核材料编排，不调用模型。只包含获得独立使用审核的事实。</p><div class="dialog-actions"><button type="submit" class="primary">生成计划草稿</button></div></form>`);
  if(a==="accept-plan"){const p=planById(id),v=p.versions.at(-1);return openDialog("确认开始这份计划",`<p>启用「${esc(v.title)}」的第 ${v.version} 版，共 ${v.modules.length} 个模块。当前学习计划会切换到这一版。</p><div class="dialog-actions"><button class="secondary" data-action="close-dialog">再看看</button><button class="primary" data-action="confirm-plan" data-id="${id}" data-version="${v.version_id}" data-revision="${p.revision}">确认启用</button></div>`);}
  if(a==="confirm-plan")return busy(button,async()=>{await post(`/plans/${id}/accept`,{version_id:button.dataset.version,expected_revision:Number(button.dataset.revision)});$("#detail-dialog").close();await loadOverview();render();toast("学习计划已启用，开始第一个模块吧。");});
  if(a==="practice") {const m=moduleFor(button.dataset.plan,id);return openDialog("记录练习与反思",`<p>${esc(m.content.title)}</p><div class="condition">${(m.content.rubric||[]).map(esc).join("<br>")}</div><form id="practice-form" data-module="${id}"><label class="form-label" for="practice-response">用自己的话解释关键条件，或描述你的尝试</label><textarea id="practice-response" class="textarea" required maxlength="10000" placeholder="我的理解是…我尝试了…还不确定的是…"></textarea><label class="form-label" for="help-mode">这次练习使用了哪些帮助？</label><select id="help-mode" class="select"><option value="assisted">参考了讲解或使用了 AI 帮助</option><option value="self_declared_independent">我声明独立完成</option><option value="unknown">暂不确定</option></select><p class="form-help">保存的是学习记录，不会自动判定掌握，也不会执行代码。</p><button type="submit" class="primary">保存练习记录</button></form>`);}
  if(a==="extract")return openDialog("提取岗位要求",`<p>将通过当前中转站 ${esc(state.data.model)} 分析岗位原文，保留来源与要求边界。</p><p>本次最多 3 次模型调用，结束后自动撤销本次调用授权。</p><div class="dialog-actions"><button class="secondary" data-action="close-dialog">取消</button><button class="primary" data-action="confirm-extract" data-id="${id}">确认提取</button></div>`);
  if(a==="confirm-extract")return busy(button,async()=>{await post(`/goals/${id}/extract`,{confirm:"AUTHORIZE",request_id:crypto.randomUUID()});$("#detail-dialog").close();await loadOverview();await navigate("tasks");toast("已提交岗位要求提取。");});
  if(a==="refresh-tasks"){state.runs=await api("/runs");return render();}
  if(a==="task-detail")return busy(button,async()=>{const r=await api("/runs/"+id), summary=state.runs.find(x=>x.id===id);openDialog("任务执行详情",`<p>${badge(r.status)}</p><h3>${esc(workflowName[r.workflow]||r.workflow)}</h3><p>已使用 ${r.calls_consumed} / ${r.call_cap} 次模型调用。</p><p>任务执行结束不代表问题已经完整解答，请以对话中的讲解和证据缺口为准。</p>${summary?.roles.length?`<h3>模型处理步骤</h3>${summary.roles.map(x=>`<div class="resource-item"><span>${esc({tutor:"生成讲解",answer_checker:"核对回答依据",coordinator:"识别学习意图",extract:"提取岗位要求",query_rewriter:"整理检索问题"}[x.role]||x.role)}</span>${badge(x.status)}</div>`).join("")}`:""}<div class="dialog-actions"><button class="primary" data-action="close-dialog">知道了</button></div>`,esc(id));});
}

document.addEventListener("click",async event=>{
  const button=event.target.closest("button");if(!button)return;
  try{
    if(button.dataset.view)return await navigate(button.dataset.view);
    if(button.dataset.action)return await handleAction(button);
    if(button.dataset.conversation)return await openConversation(button.dataset.conversation);
    if(button.dataset.filter){state.filter=button.dataset.filter;render();}
  }catch(e){error(e.message);}
});
document.addEventListener("change",event=>{
  if(event.target.dataset.select){preserveDraft();const id=event.target.dataset.select;if(event.target.checked)state.selected.add(id);else state.selected.delete(id);render();}
  if(event.target.id==="agent-mode")state.agent=event.target.value;
  if(event.target.id==="use-model")state.useModel=event.target.checked;
});
document.addEventListener("input",event=>{
  if(event.target.id==="question")state.draft=event.target.value;
  if(event.target.id==="knowledge-search"){const pos=event.target.selectionStart;state.search=event.target.value;render();const input=$("#knowledge-search");input.focus();input.setSelectionRange(pos,pos);}
});
document.addEventListener("keydown",event=>{
  if(event.target.id==="question" && event.key==="Enter" && !event.shiftKey && !event.isComposing){event.preventDefault();$("#chat-form").requestSubmit();}
  if(event.key.toLowerCase()==="n" && !event.ctrlKey && !event.metaKey && !["INPUT","TEXTAREA","SELECT"].includes(event.target.tagName) && !$("#detail-dialog").open){event.preventDefault();newChat();}
});
document.addEventListener("submit",event=>{
  const form=event.target;
  if(form.id==="chat-form")return void sendQuestion(event);
  event.preventDefault();const button=$("button[type=submit]",form);
  if(form.id==="plan-form")return void busy(button,async()=>{await post("/plans",{publication_ids:[...state.selected],learning_intent:$("#plan-intent").value,title:$("#plan-title").value});state.selected.clear();$("#detail-dialog").close();await loadOverview();await navigate("plans");toast("计划草稿已生成，请检查模块后确认。");});
  if(form.id==="goal-form")return void busy(button,async()=>{await post("/goals",{title:$("#job-title").value,text:$("#job-text").value,request_id:crypto.randomUUID()});state.goals=await api("/goals");render();toast("岗位原文已保存。");});
  if(form.id==="practice-form")return void busy(button,async()=>{await post("/observations",{module_id:form.dataset.module,response:$("#practice-response").value,rubric:{},help_mode:$("#help-mode").value});$("#detail-dialog").close();await loadOverview();render();toast("练习记录已保存；能力掌握状态未自动改变。");});
});
$("#new-chat").addEventListener("click",newChat);
$("#menu-toggle").addEventListener("click",()=>$("#sidebar").classList.toggle("open"));
$("#refresh-history").addEventListener("click",()=>loadOverview().catch(e=>error(e.message)));
$("#refresh-page").addEventListener("click",async()=>{try{preserveDraft();await loadOverview();await navigate(state.view);toast("数据已更新。");}catch(e){error(e.message);}});
$("#profile").addEventListener("click",()=>{if(state.data)openDialog("我的学习空间",`<p>当前使用本机账户：${esc(state.data.name)}</p><p>模型：${esc(state.data.model || "未配置")}</p><p>工作台只在本机访问。模型密钥和本机登录凭据保留在服务端，不传到浏览器。</p><p>每次 AI 提问最多 8 次模型调用，明确的知识构建/调研最多 32 次，完成后撤销当次授权。关闭 AI 仍可提交无需模型的学习动作。</p>`);});
window.addEventListener("hashchange",()=>navigate(location.hash.slice(1)));
hydrate();
$("#main-content").innerHTML='<div class="loading-page"><span class="spinner"></span>正在打开你的学习空间…</div>';
(async()=>{try{await loadOverview();if(!state.data.model_configured)state.useModel=false;await navigate(location.hash.slice(1)||"chat");pollTimer=setInterval(poll,2500);}catch(e){$("#connection-label").textContent="服务连接失败";$("#connection-dot").classList.add("offline");error("连接工作台失败："+e.message);$("#main-content").innerHTML=empty("学习空间暂时无法连接","检查本地服务后，点击右上角刷新重试。","","activity");}})();
