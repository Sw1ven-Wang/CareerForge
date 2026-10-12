<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, mutation } from '../api/client'
import type { components } from '../types/generated_api'
type GoalInput = components['schemas']['GoalInput']
type Goal = GoalInput & { goal_id: string; goal_version_id: string; version: number }
const goals = ref<Goal[]>([]), showGoal = ref(false), busy = ref(false), editing = ref<Goal | null>(null)
const form = reactive({ role_direction: 'Agent 开发', job_type: 'unspecified' as GoalInput['job_type'], region: '', weekly: '', reason: '' })
const saveMutation = mutation()
async function load() { try { goals.value = await api('/career-goals') } catch (error) { ElMessage.error(String(error)) } }
onMounted(load)
function openGoal(goal?: Goal) {
  editing.value = goal ?? null
  form.role_direction = goal?.role_direction ?? 'Agent 开发'
  form.job_type = goal?.job_type ?? 'unspecified'
  form.region = goal?.regions?.join('、') ?? ''
  form.weekly = goal?.time_budget?.weekly_hours == null ? '' : String(goal.time_budget.weekly_hours)
  form.reason = ''; showGoal.value = true
}
async function save() {
  busy.value = true
  const payload: GoalInput = { role_direction: form.role_direction, job_type: form.job_type,
    regions: form.region ? form.region.split(/[、,，]/).map(x => x.trim()).filter(Boolean) : [],
    time_budget: { weekly_hours: form.weekly === '' ? null : Number(form.weekly), target_date: null, reserved_fraction: 0.2 } }
  try {
    await saveMutation(editing.value ? `/career-goals/${editing.value.goal_id}/versions` : '/career-goals', 'POST',
      editing.value ? { ...payload, expected_version: editing.value.version, change_reason: form.reason } : payload)
    showGoal.value = false; await load(); ElMessage.success('目标已保存')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '保存失败') }
  finally { busy.value = false }
}
</script>
<template>
  <section class="page-heading"><div><span class="eyebrow">WORKBENCH</span><h1>当前工作台</h1><p>从你已有的材料开始，一次完成一个可以验收的目标。</p></div><el-button type="primary" @click="openGoal()">＋ 设置职业目标</el-button></section>
  <section class="hero-card"><div><span class="eyebrow">第一步 · 建立真实依据</span><h2>你想从哪里开始？</h2><p>先保存岗位原文，再对照自己的材料。未提供证据，不等于没有能力。</p><router-link to="/sources"><el-button type="primary">导入一份 JD →</el-button></router-link></div><div class="hero-art" aria-hidden="true"><div class="art-card">岗位要求 <i>✓ 原文可追溯</i></div><div class="art-line"></div><div class="art-card offset">实践成果 <i>◇ 条件可核查</i></div></div></section>
  <div class="section-title"><h2>职业目标</h2><span>{{ goals.length }} 个目标</span></div>
  <el-empty v-if="!goals.length" description="还没有职业目标。可以先设置方向，也可以直接导入已有 JD。" />
  <div v-else class="goal-grid"><article v-for="goal in goals" :key="goal.goal_id" class="goal-card"><div class="card-top"><el-tag effect="plain">目标版本 {{ goal.version }}</el-tag><el-button link @click="openGoal(goal)">修订目标</el-button></div><h3>{{ goal.role_direction }}</h3><p>{{ goal.regions?.join('、') || '地区未限定' }} · {{ goal.time_budget?.weekly_hours ? `每周 ${goal.time_budget.weekly_hours} 小时` : '投入时间待补充' }}</p><small>内容版本 {{ goal.goal_version_id.slice(0, 8) }}</small></article></div>
  <aside class="notice"><strong>当前可用</strong><span>目标设置、文本附件导入、岗位要求离线分析、来源引用与运行恢复。学习规划与成果验收按阶段开发。</span></aside>
  <el-dialog v-model="showGoal" :title="editing ? '修订为新的目标版本' : '设置职业目标'" width="520px">
    <el-form label-position="top"><el-form-item label="岗位方向"><el-input v-model="form.role_direction" /></el-form-item><el-form-item label="招聘类别"><el-select v-model="form.job_type"><el-option label="暂未限定" value="unspecified"/><el-option label="实习" value="internship"/><el-option label="初级岗位" value="entry_level"/><el-option label="有经验岗位" value="experienced"/></el-select></el-form-item><el-form-item label="地区（可留空，用顿号分隔）"><el-input v-model="form.region"/></el-form-item><el-form-item label="每周投入小时（未知可留空）"><el-input v-model="form.weekly" type="number" min="1" max="168" placeholder="未提供，留空即可"/></el-form-item><el-form-item v-if="editing" label="修改原因"><el-input v-model="form.reason" /></el-form-item></el-form><template #footer><el-button @click="showGoal = false">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存目标</el-button></template>
  </el-dialog>
</template>
