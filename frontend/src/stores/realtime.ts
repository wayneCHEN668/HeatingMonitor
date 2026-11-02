import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'
import { getAuthToken, getData } from '@/api/http'
import type { Meta, Snapshot } from '@/api/types'

/** 实时数据：WebSocket 推送（携带登录令牌，服务端按数据范围过滤），断线自动重连，期间回退到 HTTP 轮询 */
export const useRealtime = defineStore('realtime', () => {
  const meta = shallowRef<(Meta & { scope?: { global: boolean; name: string } }) | null>(null)
  const snap = shallowRef<Snapshot | null>(null)
  const connected = ref(false)
  const lastUpdate = ref(0)
  let ws: WebSocket | null = null
  let retry = 0
  let pollTimer: number | undefined
  let retryTimer: number | undefined
  let running = false

  async function loadMeta() {
    if (!meta.value) meta.value = await getData<Meta>('/api/meta')
    return meta.value
  }

  function apply(s: Snapshot) {
    snap.value = s
    lastUpdate.value = Date.now()
  }

  async function poll() {
    try {
      apply(await getData<Snapshot>('/api/realtime'))
    } catch {
      /* 后端不可用时保持上次数据 */
    }
  }

  function connect() {
    if (!running) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}/ws/realtime?token=${encodeURIComponent(getAuthToken())}`)
    ws.onopen = () => {
      connected.value = true
      retry = 0
      if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = undefined
      }
    }
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'snapshot') apply(msg.data)
      else if (msg.type === 'auth_error') {
        running = false   // 令牌失效：停止重连，由 HTTP 401 处理跳转登录
        poll()
      }
    }
    ws.onclose = () => {
      connected.value = false
      if (!running) return
      if (!pollTimer) pollTimer = window.setInterval(poll, 3000)
      retryTimer = window.setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++))
    }
    ws.onerror = () => ws?.close()
  }

  async function start() {
    if (running) return
    running = true
    await loadMeta()
    await poll()
    connect()
  }

  /** 退出登录 / 切换用户时清空，避免把上一个用户的数据范围带给下一个用户 */
  function stop() {
    running = false
    clearInterval(pollTimer)
    clearTimeout(retryTimer)
    pollTimer = undefined
    ws?.close()
    ws = null
    meta.value = null
    snap.value = null
    connected.value = false
  }

  const stationMeta = computed(() => Object.fromEntries((meta.value?.stations ?? []).map((s) => [s.id, s])))
  const sourceMeta = computed(() => Object.fromEntries((meta.value?.sources ?? []).map((s) => [s.id, s])))
  const alarmCount = computed(() => snap.value?.alarms.filter((a) => !a.acked).length ?? 0)

  return { meta, snap, connected, lastUpdate, start, stop, loadMeta, stationMeta, sourceMeta, alarmCount, poll }
})
