export interface RunEvent {
  run_id: string; seq: number; schema_version: number; type: string
  payload: Record<string, unknown>; created_at: string
}
export async function followRun(
  runId: string, receive: (event: RunEvent) => void,
  failed: (message: string) => void, signal: AbortSignal,
) {
  let after = 0
  while (!signal.aborted) {
    try {
      const response = await fetch(`/api/v1/runs/${runId}/events?after_seq=${after}`, {
        credentials: 'same-origin', headers: { Accept: 'text/event-stream' }, signal,
      })
      if (!response.ok) {
        failed(response.status === 410 ? '历史事件已清理，请刷新运行详情。' : '事件连接不可用，请刷新后重试。')
        return
      }
      const reader = response.body!.getReader(), decoder = new TextDecoder()
      let pending = ''
      while (!signal.aborted) {
        const { value, done } = await reader.read()
        pending += decoder.decode(value, { stream: !done })
        let boundary: number
        while ((boundary = pending.indexOf('\n\n')) >= 0) {
          const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2)
          if (frame.includes('event: history_expired')) {
            failed('历史事件已清理，请刷新运行详情。'); await reader.cancel(); return
          }
          const data = frame.split('\n').find(line => line.startsWith('data: '))
          if (data) {
            const event = JSON.parse(data.slice(6)) as RunEvent
            if (event.run_id === runId && Number.isInteger(event.seq) && event.seq > after) {
              if (event.seq !== after + 1) {
                failed('事件记录存在缺口，请刷新运行详情。'); await reader.cancel(); return
              }
              after = event.seq; receive(event)
            }
          }
        }
        if (done) break
      }
    } catch {
      if (signal.aborted) return
    }
    // The database remains authoritative when the network is unavailable.
    await new Promise<void>(resolve => {
      const timer = setTimeout(done, 2000)
      function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve() }
      signal.addEventListener('abort', done, { once: true })
    })
  }
}
