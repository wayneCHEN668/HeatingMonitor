import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'
import { getData } from '@/api/http'
import type { Meta, Snapshot } from '@/api/types'

/** 实时数据：WebSocket 推送，断线自动重连，期间回退到 HTTP 轮询 */
export const useRealtime = defineStore('realtime', () => {
  const meta = shallowRef<Meta | null>(null)
  const snap = shallowRef<Snapshot | null>(null)
  const connected = ref(false)
  const lastUpdate = ref(0)
  let ws: WebSocket | null = null
  let retry = 0
  let pollTimer: number | undefined

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
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}/ws/realtime`)
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
    }
    ws.onclose = () => {
      connected.value = false
      if (!pollTimer) pollTimer = window.setInterval(poll, 3000)
      const delay = Math.min(15000, 1000 * 2 ** retry++)
      setTimeout(connect, delay)
    }
    ws.onerror = () => ws?.close()
  }

  async function start() {
    await loadMeta()
    await poll()
    connect()
  }

  const stationMeta = computed(() => Object.fromEntries((meta.value?.stations ?? []).map((s) => [s.id, s])))
  const sourceMeta = computed(() => Object.fromEntries((meta.value?.sources ?? []).map((s) => [s.id, s])))
  const alarmCount = computed(() => snap.value?.alarms.filter((a) => !a.acked).length ?? 0)

  return { meta, snap, connected, lastUpdate, start, loadMeta, stationMeta, sourceMeta, alarmCount, poll }
})
