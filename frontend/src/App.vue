<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from './api/client'
import { useSession } from './stores/session'
const session = useSession()
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
    <section class="login-intro"><div class="brand"><span class="brand-mark">C</span> CareerForge</div>
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
  <div v-else class="app-shell">
    <aside class="sidebar"><div class="brand"><span class="brand-mark">C</span> CareerForge</div><p class="sidebar-caption">职业实践工作台</p>
      <nav><router-link to="/">◈ 当前工作台</router-link><router-link to="/sources">▤ 岗位资料与来源</router-link><router-link to="/runs">◷ 运行记录</router-link></nav>
      <div class="sidebar-bottom"><span class="status-dot"></span> 个人空间<br><small>{{ session.user.email }}</small><el-button link @click="logout">退出登录</el-button></div>
    </aside>
    <main class="main-content"><header class="topbar"><span>把学习变成可以核查的成果</span><el-tag type="info" effect="plain">开发预览 · S02</el-tag></header><router-view /></main>
  </div>
</template>
