import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiPage } from '../api/client'
export interface Run {
  run_id: string; run_type: string; status: string; version: number; workflow_version: string
  input_manifest_hash: string; created_at: string; updated_at: string
  usage: { call_limit: number; calls_reserved: number; held_calls?:number }
  family?:{parent_run_id:string|null;root_run_id:string;held_calls:number;available_calls:number;consumed_calls:number;accounting_note:string;children:{run_id:string;task_key:string;status:string;allocated_calls:number;consumed_calls:number;held_calls:number;released_calls:number;settled:boolean;usage:{status:string;usage:{input_tokens?:number;output_tokens?:number}|null}[]}[]}
  stage_summary: { stage_key: string; status: string }[]
  waiting_input: null | { wait_request_id: string; question: { text?: string; synthetic?: boolean; draft?: { draft?: string } }
    response_schema: { properties: { choice: { enum: string[] } } } }
}
export const useRuns = defineStore('runs', () => {
  const items = ref<Run[]>([]), nextCursor = ref<string | null>(null)
  let lastFilter = ''
  async function reload(status = '') {
    lastFilter = status
    const page = await apiPage<Run>('/runs' + (status ? `?status=${encodeURIComponent(status)}` : ''))
    items.value = page.data; nextCursor.value = page.next_cursor ?? null
  }
  async function more() {
    if (!nextCursor.value) return
    const page = await apiPage<Run>(`/runs?cursor=${encodeURIComponent(nextCursor.value)}` +
      (lastFilter ? `&status=${encodeURIComponent(lastFilter)}` : ''))
    const seen = new Set(items.value.map(run => run.run_id))
    items.value.push(...page.data.filter(run => !seen.has(run.run_id)))
    nextCursor.value = page.next_cursor ?? null
  }
  return { items, nextCursor, reload, more }
})
