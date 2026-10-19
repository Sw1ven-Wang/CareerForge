<script setup lang="ts">
import {computed} from 'vue'
const props=defineProps<{text:string}>()
const keywords=new Set('async await def class return if elif else for while in not and or is None True False try except finally raise with as from import pass yield lambda global nonlocal assert break continue let const var function export default new throw catch this extends public private protected static void null true false interface type implements package func go defer struct fn impl use pub mut match self Self super enum switch case do instanceof typeof'.split(' '))
const tokens=computed(()=>{
 const value=props.text||' ',parts:{text:string;kind:string}[]=[]
 const pattern=/("[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'|#.*$|\/\/.*$|\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b|\b[A-Za-z_]\w*\b)/g
 let end=0
 for(const match of value.matchAll(pattern)){
  const start=match.index??0,text=match[0]
  if(start>end)parts.push({text:value.slice(end,start),kind:''})
  const kind=text.startsWith('"')||text.startsWith("'")?'string':text.startsWith('#')||text.startsWith('//')?'comment':/^\d/.test(text)?'number':keywords.has(text)?'keyword':/^\s*\(/.test(value.slice(start+text.length))?'function':''
  parts.push({text,kind});end=start+text.length
 }
 if(end<value.length)parts.push({text:value.slice(end),kind:''})
 return parts
})
</script>
<template><code class="code-syntax"><span v-for="(token,i) in tokens" :key="i" :class="token.kind">{{token.text}}</span></code></template>
<style scoped>.code-syntax{font:inherit;color:inherit;white-space:pre}.keyword{color:#b8a0ff}.string{color:#9bd8b4}.number{color:#f1bd83}.comment{color:#7390aa}.function{color:#87c7f5}</style>
