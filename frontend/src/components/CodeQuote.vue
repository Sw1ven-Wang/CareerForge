<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api/client'
const props=defineProps<{citationId:string}>()
const quote=ref<{commit:string;path:string;start_line:number;end_line:number;quote:string}|null>(null),error=ref('')
watch(()=>props.citationId,async id=>{try{quote.value=await api('/citations/'+id);error.value=''}catch(e){quote.value=null;error.value=String(e)}},{immediate:true})
</script>
<template><el-alert v-if="error" :title="error" type="warning" :closable="false"/><div v-if="quote" class="code-quote"><p><strong>{{quote.path}}</strong> · {{quote.start_line}}–{{quote.end_line}}<br><small>commit {{quote.commit}}</small></p><pre>{{quote.quote}}</pre><small>原始文本定位；未执行代码。</small></div></template>
<style scoped>.code-quote{margin:12px 0;padding:16px;background:#f4f6f8;border-radius:8px}.code-quote pre{white-space:pre;overflow:auto;max-height:420px;font-size:12px;line-height:1.6}</style>
