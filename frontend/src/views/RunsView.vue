<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRuns, type Run } from '../stores/runs'
import RunPanel from '../components/RunPanel.vue'
function selectRun(row: Run) { selected.value = row.run_id }
const runs = useRuns(), selected = ref(''), error = ref(''), filter = ref('')
async function reload() {
  try { await runs.reload(filter.value); error.value = '' }
  catch (e) { error.value = String(e) }
}
onMounted(() => { void reload() })
</script>
<template>
  <div class="page-heading"><span class="eyebrow">研究过程与待处理决定</span><h1>运行记录</h1><p>进度来自已保存的记录。任务等待你的决定时，会出现在这里。</p></div>
  <el-select v-model="filter" @change="reload" placeholder="全部状态" clearable style="width:180px;margin-bottom:20px">
    <el-option label="待你确认" value="waiting_user"/><el-option label="执行中" value="running"/><el-option label="已完成" value="succeeded"/>
  </el-select>
  <el-button @click="reload" style="margin-left:12px">刷新记录</el-button>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <el-empty v-if="!runs.items.length && !error" description="暂无运行记录，开始研究后可在这里查看。" />
  <el-table v-else :data="runs.items" @row-click="selectRun" style="cursor:pointer;margin-bottom:24px">
    <el-table-column label="研究类型"><template #default="{row}">{{ ({market_research:'岗位分析',alignment:'目标对齐',project_research:'项目研究',learning_path:'代码学习',assessment:'成果验收'} as Record<string,string>)[row.run_type] ?? '研究任务' }}</template></el-table-column>
    <el-table-column label="状态"><template #default="{row}">{{ ({queued:'排队中',running:'执行中',waiting_user:'待你确认',succeeded:'已完成',partially_succeeded:'部分完成',failed:'失败',budget_stopped:'预算已用尽',cancelled:'已取消'} as Record<string,string>)[row.status] ?? '待更新' }}</template></el-table-column>
    <el-table-column label="创建时间"><template #default="{row}">{{new Date(row.created_at).toLocaleString()}}</template></el-table-column>
  </el-table>
  <el-button v-if="runs.nextCursor" @click="runs.more().catch(e => error = String(e))" style="margin-bottom:24px">读取更早记录</el-button>
  <RunPanel v-if="selected" :key="selected" :run-id="selected" @changed="reload" />
</template>
