<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { api } from './api/client'
import { useSession } from './stores/session'
const session = useSession()
const route = useRoute()
const studio = computed(() => route.path === '/studio')
const menuOpen = ref(false)
const credentials = reactive({ email: '', password: '' })
const registering = ref(false), busy = ref(false)
onMounted(() => session.refresh())
async function submit() {
  busy.value = true
  try {
    if (registering.value) await api('/auth/register', 'POST', credentials)
    await session.login(credentials)
    credentials.password = ''
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '登录失败') }
  finally { busy.value = false }
}
async function logout() {
  try { await session.logout() } catch (error) { ElMessage.error(String(error)) }
}
</script>
<template>
  <div v-if="!session.ready" class="loading">正在读取工作台…</div>
  <main v-else-if="!session.user" class="login-shell">
    <section class="login-intro"><div class="brand"><span class="brand-mark" aria-hidden="true">✦</span> CareerForge</div>
      <span class="eyebrow">从岗位要求，到可展示的成果</span><h1>把下一步学习，<br>变成一份可信证据。</h1>
      <p>保存真实材料，明确学习目标，在自己的项目里完成实践。每项判断，都有出处和适用范围。</p>
      <div class="intro-flow">岗位材料 <span>→</span> 学习目标 <span>→</span> 实践证据</div>
    </section>
    <section class="login-card"><span class="eyebrow">你的个人工作空间</span><h2>{{ registering ? '创建账号' : '欢迎回来' }}</h2>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="邮箱"><el-input v-model="credentials.email" type="email" autocomplete="email" required /></el-form-item>
        <el-form-item label="密码（至少 10 个字符）"><el-input v-model="credentials.password" type="password" show-password :autocomplete="registering ? 'new-password' : 'current-password'" required /></el-form-item>
        <el-button type="primary" native-type="submit" :loading="busy" class="full-width">{{ registering ? '创建并进入工作台' : '登录工作台' }}</el-button>
      </el-form><el-button link class="auth-switch" @click="registering = !registering">{{ registering ? '已有账号，去登录' : '第一次使用？创建账号' }}</el-button>
    </section>
  </main>
  <div v-else class="app-shell" :class="{'studio-shell':studio,'menu-open':menuOpen}">
    <aside v-if="!studio" class="sidebar"><router-link to="/agent" class="brand"><span class="brand-mark" aria-hidden="true">✦</span><span>CareerForge<small>BUILD SKILLS FROM CODE</small></span></router-link><p class="sidebar-caption">个人 Agent 学习工作台</p>
      <nav aria-label="工作台导航" @click="menuOpen=false"><span class="navigation-label">工作空间</span>
        <router-link to="/agent">◈ Agent 工作台</router-link>
        <router-link to="/sources">▤ 需求资料</router-link>
        <router-link to="/knowledge">⌘ 开源项目</router-link>
        <details class="sidebar-group"><summary>更多</summary><div class="sidebar-group-links">
          <router-link to="/studio">学习 IDE</router-link>
          <router-link to="/planning">历史学习计划</router-link>
          <router-link to="/market">岗位统计与分析</router-link>
          <router-link to="/goals">学习方向设置</router-link>
          <router-link to="/runs">运行记录</router-link>
        </div></details>
      </nav>
      <div class="sidebar-bottom"><span class="status-dot"></span> 个人空间<br><small>{{ session.user.email }}</small><el-button link @click="logout">退出登录</el-button></div>
    </aside>
    <main class="main-content"><header v-if="!studio" class="topbar"><button class="mobile-menu" aria-label="展开导航" @click="menuOpen=!menuOpen">☰</button><span>工作空间 <i>/</i> {{route.path==='/sources'?'需求资料':route.path==='/knowledge'?'开源项目':route.path==='/runs'?'运行记录':'Agent 学习工作台'}}</span><router-link class="open-studio" to="/studio">打开学习工作台 ↗</router-link></header><router-view :key="studio ? String(route.query.track??'library') : route.path"/></main>
  </div>
</template>

<style scoped>
.sidebar-group{min-width:0;border-radius:8px}
.sidebar-group summary{padding:13px 15px;cursor:pointer;font-size:14px;color:#bdd0cb;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:8px}
.sidebar-group summary::-webkit-details-marker{display:none}
.sidebar-group summary::after{content:'›';transition:transform .15s}
.sidebar-group[open] summary::after{transform:rotate(90deg)}
.sidebar-group:has(.router-link-exact-active)>summary{color:#ebf5f0}
.sidebar-group-links{display:grid;gap:5px;margin:0 0 4px 18px;padding-left:8px;border-left:1px solid #355653}
.sidebar-group-links a{font-size:13px;padding:11px 13px}
@media(max-width:720px){.sidebar-group{grid-column:1 / -1}.sidebar-group-links{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}}
</style>
