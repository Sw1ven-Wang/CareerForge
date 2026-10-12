import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiPage } from '../api/client'
export interface Run {
  run_id: string; run_type: string; status: string; version: number; workflow_version: string
  input_manifest_hash: string; created_at: string; updated_at: string
  usage: { call_limit: number; calls_reserved: number }
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
