import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, setAuthToken, setUnauthorizedHandler } from '@/api/http'
import type { Me } from '@/api/types'

const KEY = 'heatnet_token'

function readToken(): string {
  try {
    return localStorage.getItem(KEY) || ''
  } catch {
    return ''
  }
}
function writeToken(t: string) {
  try {
    if (t) localStorage.setItem(KEY, t)
    else localStorage.removeItem(KEY)
  } catch {
    /* 隐私模式等情况下不可用：仅本次会话有效 */
  }
}

/** 登录状态。令牌只用于本浏览器的会话保持；权限判断以服务端为准，前端只做界面显隐。 */
export const useAuth = defineStore('auth', () => {
  const token = ref(readToken())
  const me = ref<Me | null>(null)
  const expiredMsg = ref('')
  setAuthToken(token.value)

  const loggedIn = computed(() => !!token.value && !!me.value)
  const has = (perm: string) => !!me.value?.permissions.includes(perm)
  const hasAny = (...perms: string[]) => perms.some(has)

  function apply(t: string) {
    token.value = t
    setAuthToken(t)
    writeToken(t)
  }

  async function login(username: string, password: string) {
    const r = await api.post<{ token: string; user: Me }>('/api/auth/login', { username, password })
    apply(r.data.token)
    me.value = r.data.user
    expiredMsg.value = ''
    return r.data.user
  }

  /** 启动时用已保存的令牌恢复会话 */
  async function restore() {
    if (!token.value) return false
    try {
      me.value = (await api.get<Me>('/api/auth/me')).data
      return true
    } catch {
      apply('')
      me.value = null
      return false
    }
  }

  async function logout() {
    try {
      if (token.value) await api.post('/api/auth/logout')
    } catch {
      /* 已失效也无妨 */
    }
    apply('')
    me.value = null
  }

  function expire(msg: string) {
    if (!token.value) return
    apply('')
    me.value = null
    expiredMsg.value = msg
  }
  setUnauthorizedHandler(expire)

  return { token, me, expiredMsg, loggedIn, has, hasAny, login, restore, logout, expire }
})
