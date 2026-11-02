import type { Envelope } from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    public code?: string,
    public traceId?: string,
  ) {
    super(message)
  }
}

/** 当前登录令牌与 401 处理回调（由 auth store 注入，避免循环依赖） */
let authToken = ''
let onUnauthorized: (msg: string) => void = () => {}
export function setAuthToken(t: string) {
  authToken = t
}
export function getAuthToken() {
  return authToken
}
export function setUnauthorizedHandler(fn: (msg: string) => void) {
  onUnauthorized = fn
}

async function request<T>(method: string, url: string, body?: unknown): Promise<Envelope<T>> {
  const headers: Record<string, string> = {}
  if (body) headers['Content-Type'] = 'application/json'
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined })
  let json: any
  try {
    json = await res.json()
  } catch {
    throw new ApiError(`服务异常（HTTP ${res.status}）`)
  }
  if (res.status === 401) {
    const msg = json?.error?.message || '登录已失效'
    onUnauthorized(msg)
    throw new ApiError(msg, json?.error?.code, json?.trace_id)
  }
  if (!res.ok || json.ok === false) {
    const msg = json?.error?.message || json?.summary || json?.detail || `请求失败（HTTP ${res.status}）`
    throw new ApiError(msg, json?.error?.code, json?.trace_id)
  }
  return json as Envelope<T>
}

/** 返回完整封套（需要 summary / evidence / warnings 时使用） */
export const api = {
  get: <T>(url: string) => request<T>('GET', url),
  post: <T>(url: string, body?: unknown) => request<T>('POST', url, body ?? {}),
  del: <T>(url: string) => request<T>('DELETE', url),
}

/** 只取 data */
export const getData = async <T>(url: string) => (await api.get<T>(url)).data
export const postData = async <T>(url: string, body?: unknown) => (await api.post<T>(url, body)).data

export function qs(params: Record<string, string | number | undefined | null>) {
  const s = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join('&')
  return s ? `?${s}` : ''
}
