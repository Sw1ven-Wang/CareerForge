<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import type {StudyModule} from '../types/study'
defineOptions({name:'StudyOutline'})
const props=withDefaults(defineProps<{modules:StudyModule[];selected:string;parent?:string|null;level?:number;step?:number}>(),{parent:null,level:0,step:0})
const emit=defineEmits<{select:[key:string];step:[key:string,index:number]}>()
const opened=ref(new Set<string>())
const rows=computed(()=>props.modules.filter(m=>props.parent?m.parent_key===props.parent:!m.parent_key||!props.modules.some(p=>p.key===m.parent_key)))
function children(key:string){return props.modules.some(m=>m.parent_key===key)}
function toggle(key:string){const next=new Set(opened.value);next.has(key)?next.delete(key):next.add(key);opened.value=next}
watch(()=>props.selected,key=>{const next=new Set(opened.value);let item=props.modules.find(m=>m.key===key),count=0;while(item?.parent_key&&count++<3){next.add(item.parent_key);item=props.modules.find(m=>m.key===item?.parent_key)}if(item&&children(item.key))next.add(item.key);opened.value=next},{immediate:true})
</script>
<template><div class="outline" :role="level?'group':'tree'" :aria-label="level?undefined:'分级学习大纲'">
<div v-for="(m,index) in rows" :key="m.key" class="outline-item" role="treeitem" :aria-selected="selected===m.key" :aria-expanded="children(m.key)?opened.has(m.key):undefined">
<div class="outline-row" :class="{selected:selected===m.key,group:children(m.key)}"><button v-if="children(m.key)" class="outline-chevron" :aria-label="(opened.has(m.key)?'收起':'展开')+m.title" @click="toggle(m.key)">{{opened.has(m.key)?'⌄':'›'}}</button><span v-else class="outline-dot"/><button class="outline-title" :title="m.title" @click="emit('select',m.key)"><span v-if="!level&&children(m.key)" class="outline-index">{{String(index+1).padStart(2,'0')}} · </span>{{m.title}}</button><span class="outline-depth" :title="'目标深度：'+m.depth+'，不代表本人已掌握'">{{m.depth}}</span></div>
<ol v-if="selected===m.key&&m.steps.length" class="outline-steps"><li v-for="(text,i) in m.steps" :key="i"><button :class="{current:step===i}" :title="text" @click="emit('step',m.key,i)">{{i+1}}. {{text}}</button></li></ol>
<StudyOutline v-if="children(m.key)&&opened.has(m.key)" class="outline-children" :modules="modules" :selected="selected" :parent="m.key" :level="level+1" :step="step" @select="key=>emit('select',key)" @step="(key,index)=>emit('step',key,index)"/>
</div></div></template>
<style scoped>
.outline{display:grid;gap:3px}.outline-row{display:flex;align-items:center;gap:6px;min-height:35px;border-radius:7px;padding:3px 3px 3px 5px;color:#7d8aa0}.outline-row.group{color:#45546e;font-weight:650;margin-top:5px}.outline-row:hover{background:#eeeffa}.outline-row.selected{background:#fff;color:#6050dc;box-shadow:inset 2px 0 #7665ed}.outline-chevron{border:0;background:none;color:#8f97ac;padding:2px;flex:none;width:14px;font-size:17px}.outline-title{border:0;background:none;text-align:left;padding:6px 0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:inherit;font-size:11px;line-height:1.5;font-weight:inherit}.outline-depth{flex:none;font-size:9px;color:#8892a7;background:white;border:1px solid #e0e4ec;border-radius:5px;padding:2px 4px}.outline-dot{width:5px;height:5px;background:#bdc5d3;border-radius:50%;margin:0 3px;flex:none}.selected .outline-dot{background:#7665ed}.outline-children{border-left:1px solid #dfe3ed;margin:0 0 5px 11px;padding-left:9px}.outline-steps{list-style:none;margin:0 0 7px 21px;padding:0;border-left:1px dashed #d8d5ee}.outline-steps button{display:block;width:100%;padding:5px 6px;border:0;border-radius:4px;background:none;text-align:left;color:#98a4b7;font-size:10px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;line-height:1.5}.outline-steps .current{color:#7665ed;background:#efedff}.outline-index{font-size:10px}
</style>
