let csrfToken = ''
export function setCsrf(value: string) { csrfToken = value }
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message) }
}
async function request<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<{data: T; next_cursor?: string | null}> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (method !== 'GET') {
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken
    if (key) headers['Idempotency-Key'] = key
  }
  const response = await fetch('/api/v1' + path, {
    method, credentials: 'same-origin', headers, body: body === undefined ? undefined : JSON.stringify(body),
  })
  const value = await response.json()
  if (!response.ok) throw new ApiError(response.status, value.error?.code ?? 'HTTP_ERROR',
    value.error?.message ?? '请求失败，请稍后再试。')
  return value as {data: T; next_cursor?: string | null}
}
export async function api<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
  return (await request<T>(path, method, body, key)).data
}
export async function apiPage<T>(path: string) { return request<T[]>(path) }
// Keep the key after an uncertain response; a changed payload is a different operation.
export function mutation() {
  let previous = '', key = ''
  return async <T>(path: string, method: string, body: unknown): Promise<T> => {
    const canonical = JSON.stringify([path, method, body])
    if (canonical !== previous) { previous = canonical; key = crypto.randomUUID() }
    const result = await api<T>(path, method, body, key)
    previous = ''; key = ''
    return result
  }
}

export async function uploadAttachment(file: File, purpose = 'source', key = crypto.randomUUID()) {
  const data = new FormData(); data.append('file', file); data.append('purpose', purpose)
  const response = await fetch('/api/v1/attachments', {method:'POST', credentials:'same-origin',
    headers:{'X-CSRF-Token':csrfToken,'Idempotency-Key':key}, body:data})
  const value = await response.json()
  if (!response.ok) throw new ApiError(response.status,value.error?.code ?? 'HTTP_ERROR',value.error?.message ?? '上传失败')
  return value.data as {attachment_id:string; name:string; read_status:string}
}
