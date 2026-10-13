<script setup lang="ts">
import { computed, ref, watch } from 'vue'
export type Practice = {
  id: string; kind: string; status: string; objective_title?: string; objective_version_id?: string
  exercise?: string; citation_ids?: string[]; note?: string; synthetic_source?: boolean
}
type Related = { id: string; kind: string; status: string; practice_id?: string; exit_code?: number; argv?: string[] }
const props=defineProps<{practice:Practice;actions:Related[];selected:boolean;busy:boolean}>()
const emit=defineEmits<{resume:[];save:[note:string];open:[id:string];delivery:[]}>()
const note=ref(props.practice.note??'')
watch(()=>props.practice.note,value=>{note.value=value??''})
const related=computed(()=>props.actions.filter(a=>a.practice_id===props.practice.id&&a.kind!=='practice'))
const patches=computed(()=>related.value.filter(a=>a.kind==='patch'))
const commands=computed(()=>related.value.filter(a=>a.kind==='command'))
</script>
<template>
<section class="practice-card" :class="{selected}">
  <div class="heading"><strong>迁移练习草稿</strong><el-tag v-if="selected" size="small">当前练习</el-tag><el-tag v-if="practice.synthetic_source" size="small" type="warning">来自模拟说明</el-tag></div>
  <el-alert v-if="practice.status==='unavailable'" title="原学习目标或代码依据已不可用，此练习及相关输出已隐藏。" type="warning" :closable="false"/>
  <template v-else>
    <small>{{practice.objective_title}}</small><p class="exercise">{{practice.exercise}}</p>
    <el-button type="primary" plain :disabled="busy" @click="emit('resume')">{{selected?'回到自己的 IDE':'继续此练习'}}</el-button>
    <el-button v-for="(id,i) in practice.citation_ids" :key="id" link @click="emit('open',id)">代码依据 {{i+1}}</el-button>
    <p class="record">建议 {{related.filter(a=>a.kind==='message').length}} 次 · 补丁 {{patches.length}} 组（已应用 {{patches.filter(a=>a.status==='applied').length}}） · 命令 {{commands.length}} 条</p>
    <details v-if="commands.length"><summary>命令记录</summary><p v-for="c in commands" :key="c.id">{{JSON.stringify(c.argv)}} · {{c.status==='pending'?'待确认':c.status==='running'?'执行中':c.exit_code===undefined?'执行失败':'退出码 '+c.exit_code}}</p></details>
    <el-button :disabled="busy" @click="emit('delivery')">提交成果 / 人工验收</el-button>
    <label>练习笔记</label><el-input v-model="note" type="textarea" :rows="3" maxlength="4000" show-word-limit placeholder="记录自己的理解、执行观察和待解决问题…"/>
    <el-button size="small" :disabled="busy||note===(practice.note??'')" @click="emit('save',note)">保存笔记</el-button>
    <small>笔记、补丁和退出码只记录过程，不自动确认目标完成或个人能力。</small>
  </template>
</section>
</template>
<style scoped>
.practice-card{padding:12px;background:#f3f7f4;border:1px solid #dce8df;border-radius:8px}.practice-card.selected{border-color:#5e8d72;background:#edf5ef}.heading{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.exercise{white-space:pre-wrap;line-height:1.8}.record,small,details p{font-size:12px;color:#62786b;line-height:1.8}small,label{display:block;margin:8px 0}.el-button{margin:8px 8px 0 0;white-space:normal;height:auto;min-height:30px}details p{overflow-wrap:anywhere}summary{cursor:pointer;font-size:12px}
</style>
