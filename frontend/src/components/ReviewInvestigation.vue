<script setup lang="ts">
import {useRouter} from 'vue-router'
import type {Review} from '../types/study'
defineProps<{review:Review}>()
const router=useRouter()
const stopNames:Record<string,string>={evidence_sufficient:'模型判断证据足够',unsupported:'模型判断不支持目标',no_new_evidence:'没有新增候选或已读原文',model_budget:'已到模型调用上限',read_budget:'已到原文读取上限',tool_budget:'已到工具上限'}
</script>
<template>
<section v-if="review.investigation" class="investigation">
 <strong>有界代码调查</strong>
 <p>{{stopNames[review.investigation.stop_reason]??review.investigation.stop_reason}} · {{review.investigation.tool_count}} 次工具 · {{review.investigation.read_count}} 次读取，其中测试 {{review.investigation.test_reads}} 次</p>
 <small>每候选模型上限 {{review.investigation.model_call_limit}} 次（含最终报告），最多 {{review.investigation.budgets.tools}} 次工具、{{review.investigation.budgets.reads}} 次源码读取。所有测试均未运行。</small>
 <p v-if="review.investigation.stop_explanation">{{review.investigation.stop_explanation}}</p>
 <details><summary>已读范围与补证据轨迹</summary>
  <ul><li v-for="(r,i) in review.investigation.reads" :key="i">{{r.is_test?'测试源码':'实现源码'}} · {{r.path}}:{{r.start_line}}–{{r.end_line}} {{r.truncated?'（仅部分范围）':''}}</li></ul>
  <ol><li v-for="(step,i) in review.investigation.trace" :key="i"><code>{{step.tool}}</code> · {{step.status}}<small>{{JSON.stringify(step.arguments)}}</small></li></ol>
  <p v-if="review.investigation.index_truncated">符号/引用候选索引已截断，保留未知部分。</p>
  <p v-if="review.investigation.parse_failures.length">{{review.investigation.parse_failures.length}} 个 Python 文件未能解析，仍可按正文检索。</p>
  <small>关系仅为静态同名候选，不能作为确定调用图。快照指纹 {{review.investigation.archive_hash}}</small>
 </details>
</section>
<details v-if="review.claims?.length||review.evidence?.length" class="claims"><summary>断言与原文依据 · 语义支持待核验</summary>
 <article v-for="claim in review.claims" :key="claim.claim_id"><strong>{{claim.mechanism_id}}</strong><p>{{claim.statement}}</p><small>模型提议 · 引用位置已校验 · 语义支持未独立核验</small><p v-if="claim.counterevidence_refs.length">有 {{claim.counterevidence_refs.length}} 项潜在反证，尚未独立解决。</p></article>
 <blockquote v-for="e in review.evidence" :key="e.evidence_id"><small>{{e.purpose==='counterevidence'?'潜在反证':e.purpose==='test_source'?'测试源码（未运行）':'实现原文'}} · {{e.path}}:{{e.start_line}}–{{e.end_line}}</small><pre>{{e.quote}}</pre><el-button v-if="e.citation_id" link @click="router.push({path:'/studio',query:{citation:e.citation_id}})">查看引用位置</el-button></blockquote>
</details>
</template>
<style scoped>
.investigation{padding:12px 0;border-block:1px solid #dce5dc;font-size:12px}.investigation p,.claims p{line-height:1.7}.investigation small,.claims small{display:block;color:#75837a;overflow-wrap:anywhere}.investigation li{margin:8px 0}.claims article{padding-block:10px}.claims blockquote{margin:10px 0;padding:10px;border-left:3px solid #b4cdbb;background:#edf3ed}.claims pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer;color:#52745c}
</style>
