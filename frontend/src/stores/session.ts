import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, setCsrf } from '../api/client'
import type { components } from '../types/generated_api'
type Credentials = components['schemas']['Credentials']
export const useSession = defineStore('session', () => {
  const user = ref<{ user_id: string; email: string } | null>(null)
  const ready = ref(false)
  async function refresh() {
    try {
      user.value = await api('/auth/me')
      const csrf = await api<{ csrf_token: string }>('/auth/csrf')
      setCsrf(csrf.csrf_token)
    } catch { user.value = null; setCsrf('') }
    finally { ready.value = true }
  }
  async function login(value: Credentials) { await api('/auth/login', 'POST', value); await refresh() }
  async function logout() { await api('/auth/logout', 'POST'); user.value = null; setCsrf('') }
  return { user, ready, refresh, login, logout }
})
