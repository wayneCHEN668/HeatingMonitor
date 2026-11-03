<template>
  <svg viewBox="0 0 860 520" class="topo">
    <!-- 管线：每个站一条供水（红）+ 回水（蓝），L 形走线 -->
    <g v-for="s in meta.stations" :key="'p' + s.id">
      <path :d="route(s, 0)" class="pipe flow-line" stroke="#ff6b4a" :class="{ stop: !live(s.id)?.online }" />
      <path :d="route(s, 5)" class="pipe flow-line rev" stroke="#38a8ff" :class="{ stop: !live(s.id)?.online }" />
    </g>
    <!-- 热源 -->
    <g v-for="h in meta.sources" :key="h.id" :transform="`translate(${h.xy[0]},${h.xy[1]})`" class="node" @click="$emit('select', h.id)">
      <rect x="-58" y="-26" width="116" height="52" rx="8" fill="#2a1410" stroke="#ff6b4a" stroke-width="2" />
      <text y="-8" text-anchor="middle" class="src-name">{{ h.name.replace('首站', '').replace('调峰锅炉房', '锅炉房') }}</text>
      <text y="12" text-anchor="middle" class="src-val num">
        {{ fmt(src(h.id)?.heat_mw) }} MW · {{ fmt(src(h.id)?.load_pct, 0) }}%
      </text>
    </g>
    <!-- 热力站 -->
    <g v-for="s in meta.stations" :key="s.id" :transform="`translate(${s.xy[0]},${s.xy[1]})`" class="node" @click="$emit('select', s.id)">
      <circle r="13" :fill="statusColor(s.id)" fill-opacity=".18" :stroke="statusColor(s.id)" stroke-width="2"
              :class="{ blink: level(s.id) === 1 }" />
      <circle r="4" :fill="statusColor(s.id)" />
      <text y="-19" text-anchor="middle" class="st-name">{{ s.name }}</text>
      <text y="28" text-anchor="middle" class="st-val num">
        <tspan :fill="tinColor(live(s.id)?.tin)">{{ fmt(live(s.id)?.tin) }}℃</tspan>
        <tspan fill="#8797b3"> · {{ fmt(live(s.id)?.heat_mw, 2) }}MW</tspan>
      </text>
    </g>
    <!-- 可视化高亮（viz_render map） -->
    <g v-for="id in highlight ?? []" :key="'hl' + id" :transform="`translate(${xyOf(id)[0]},${xyOf(id)[1]})`" class="hl" :class="level">
      <circle :r="isSource(id) ? 46 : 24" class="ring" />
      <circle :r="isSource(id) ? 46 : 24" class="ring pulse" />
    </g>
    <!-- 图例 -->
    <g v-if="metricRange" transform="translate(14,466)" class="legend">
      <text x="0" y="4">{{ metricLabel }}</text>
      <rect x="64" y="-4" width="120" height="8" fill="url(#vizScale)" />
      <text x="190" y="4">{{ fmt(metricRange[0], 1) }} – {{ fmt(metricRange[1], 1) }}</text>
    </g>
    <defs>
      <linearGradient id="vizScale"><stop offset="0" stop-color="#38a8ff" /><stop offset=".5" stop-color="#22c55e" /><stop offset="1" stop-color="#ef4444" /></linearGradient>
    </defs>
    <g transform="translate(14,490)" class="legend">
      <circle cx="6" cy="0" r="5" fill="#22c55e" /><text x="16" y="4">正常</text>
      <circle cx="62" cy="0" r="5" fill="#f5a524" /><text x="72" y="4">告警</text>
      <circle cx="118" cy="0" r="5" fill="#ef4444" /><text x="128" y="4">严重</text>
      <circle cx="174" cy="0" r="5" fill="#64748b" /><text x="184" y="4">离线</text>
      <line x1="232" y1="0" x2="256" y2="0" stroke="#ff6b4a" stroke-width="3" /><text x="262" y="4">供水</text>
      <line x1="300" y1="0" x2="324" y2="0" stroke="#38a8ff" stroke-width="3" /><text x="330" y="4">回水</text>
    </g>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Meta, Snapshot, StationMeta } from '@/api/types'
import { fmt } from '@/utils/format'

const props = defineProps<{ meta: Meta; snap: Snapshot; highlight?: string[]; colorBy?: string; level?: string }>()
const METRIC_LABEL: Record<string, string> = { tin: '典型室温', heat_mw: '热功率', ts2: '二次供温', valve: '阀门开度', makeup_tph: '补水量' }
const metricLabel = computed(() => METRIC_LABEL[props.colorBy ?? ''] ?? '')
const metricRange = computed<[number, number] | null>(() => {
  if (!props.colorBy || props.colorBy === 'alarm') return null
  const vs = props.meta.stations.map((s) => (live(s.id) as any)?.[props.colorBy!]).filter((v) => typeof v === 'number') as number[]
  return vs.length ? [Math.min(...vs), Math.max(...vs)] : null
})
function scaleColor(v: number) {
  const [lo, hi] = metricRange.value!
  const t = hi > lo ? (v - lo) / (hi - lo) : 0.5
  const stops = [[56, 168, 255], [34, 197, 94], [239, 68, 68]]
  const [a, b, f] = t < 0.5 ? [stops[0], stops[1], t * 2] : [stops[1], stops[2], (t - 0.5) * 2]
  return `rgb(${a.map((x, i) => Math.round(x + (b[i] - x) * f)).join(',')})`
}
const isSource = (id: string) => props.meta.sources.some((h) => h.id === id)
const xyOf = (id: string) => (props.meta.sources.find((h) => h.id === id) ?? props.meta.stations.find((s) => s.id === id))?.xy ?? [0, 0]
defineEmits<{ (e: 'select', id: string): void }>()

const live = (id: string) => props.snap.stations[id]
const src = (id: string) => props.snap.sources[id]
const level = (id: string) => {
  const ls = props.snap.alarms.filter((a) => a.object_id === id).map((a) => a.level)
  return ls.length ? Math.min(...ls) : 0
}
const statusColor = (id: string) => {
  if (!live(id)?.online) return '#64748b'
  const mv = (live(id) as any)?.[props.colorBy ?? '']
  if (metricRange.value && typeof mv === 'number') return scaleColor(mv)
  const l = level(id)
  return l === 1 ? '#ef4444' : l === 2 ? '#f5a524' : l === 3 ? '#eab308' : '#22c55e'
}
const tinColor = (t?: number) => (t === undefined ? '#8797b3' : t < 18 ? '#ef4444' : t > 22.5 ? '#f5a524' : '#22c55e')

function route(s: StationMeta, off: number) {
  const h = props.meta.sources.find((x) => x.id === s.source_id)!
  const [x0, y0] = h.xy
  const [x1, y1] = s.xy
  const midX = x0 + (x1 > x0 ? 70 : -70) + off
  return `M${x0 + off} ${y0 + off} H${midX} V${y1 + off} H${x1 + off}`
}
</script>

<style scoped>
.topo { width: 100%; height: auto; display: block; }
.pipe { fill: none; stroke-width: 2.5; opacity: .75; }
.node { cursor: pointer; }
.node:hover circle:first-child, .node:hover rect { filter: brightness(1.4); }
.src-name { font-size: 12px; fill: #ffd8cc; font-weight: 700; }
.src-val { font-size: 11px; fill: #ffb199; }
.st-name { font-size: 11px; fill: #dbe5f4; font-weight: 600; }
.st-val { font-size: 10px; }
.legend text { font-size: 11px; fill: #8797b3; }
.hl .ring { fill: none; stroke: #3b8cff; stroke-width: 3; stroke-dasharray: 6 4; }
.hl.warn .ring { stroke: #f5a524; }
.hl.danger .ring { stroke: #ef4444; }
.hl .pulse { stroke-dasharray: none; stroke-width: 2; animation: viz-pulse 1.6s ease-out infinite; transform-origin: center; transform-box: fill-box; }
@keyframes viz-pulse { from { opacity: .9; transform: scale(1); } to { opacity: 0; transform: scale(1.6); } }
</style>
