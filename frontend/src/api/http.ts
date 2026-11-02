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

async function request<T>(method: string, url: string, body?: unknown): Promise<Envelope<T>> {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  let json: any
  try {
    json = await res.json()
  } catch {
    throw new ApiError(`服务异常（HTTP ${res.status}）`)
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
