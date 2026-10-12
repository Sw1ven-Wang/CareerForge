let csrfToken = ''
export function setCsrf(value: string) { csrfToken = value }
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message) }
}
export async function api<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
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
  return value.data as T
}
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
