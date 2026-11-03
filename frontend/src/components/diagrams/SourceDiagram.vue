<template>
  <svg viewBox="0 0 1000 560" class="diagram" preserveAspectRatio="xMidYMid meet">
    <defs>
      <linearGradient id="tankfill" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="#ef4444" />
        <stop offset="1" stop-color="#f59e0b" />
      </linearGradient>
      <pattern id="tubes" width="10" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(35)">
        <rect width="10" height="10" fill="#13213b" />
        <line x1="0" y1="0" x2="0" y2="10" stroke="#4a6491" stroke-width="3" />
      </pattern>
    </defs>

    <!-- 能源输入 -->
    <g>
      <rect x="150" y="112" width="230" height="36" rx="4" fill="#1a1a10" :stroke="energy.color" />
      <text x="160" y="128" class="en" :fill="energy.color">{{ energy.title }}</text>
      <text x="160" y="142" class="en-sub">{{ energy.sub }}</text>
      <path :d="`M380 130 H${unitXs[unitXs.length - 1] - 30}`" :stroke="energy.color" class="pipe energy flow-line" />
    </g>

    <!-- 供水母管 -->
    <path d="M300 80 H980" :stroke="cs" class="pipe flow-line" :class="stop" />
    <text x="840" y="66" class="pipe-lbl">供水 → 一级管网</text>
    <!-- 回水母管 -->
    <path d="M980 500 H110" :stroke="cr" class="pipe flow-line" :class="stop" />
    <text x="840" y="524" class="pipe-lbl">← 一级管网回水</text>
    <!-- 泵出口母管 -->
    <path :d="`M${pumpXs[0]} 380 H${unitXs[unitXs.length - 1]}`" :stroke="cr" class="pipe flow-line" :class="stop" />

    <!-- 循环泵 -->
    <g v-for="(p, i) in pumps" :key="p.id">
      <path :d="`M${pumpXs[i]} 500 V380`" :stroke="cr" class="pipe thin flow-line" :class="p.running ? '' : 'stop'" />
      <DPump :x="pumpXs[i]" :y="440" :running="p.running" />
      <text :x="pumpXs[i] + 20" y="436" class="eq">{{ p.name.replace('循环泵', '泵') }}</text>
      <text :x="pumpXs[i] + 20" y="451" class="sv" :fill="p.running ? '#7ee2a8' : '#6b7790'">
        {{ p.running ? fixed(p.freq) + 'Hz' : '停' }}
      </text>
    </g>

    <!-- 主设备 -->
    <g v-for="(u, i) in units" :key="u.id">
      <path :d="`M${unitXs[i]} 380 V${u.type === 'tank' ? 330 : 300}`" :stroke="cr" class="pipe thin flow-line" :class="u.running ? '' : 'stop'" />
      <path :d="`M${unitXs[i] + 20} ${u.type === 'tank' ? 160 : 180} V80`" :stroke="u.running ? cs : '#3b4a66'" class="pipe thin flow-line" :class="u.running ? '' : 'stop'" />
      <path v-if="u.type !== 'tank'" :d="`M${unitXs[i] - 30} 130 V180`" :stroke="energy.color" class="pipe energy" />

      <!-- 储热罐 -->
      <g v-if="u.type === 'tank'">
        <rect :x="unitXs[i] - 40" y="160" width="80" height="170" rx="16" fill="#0d1a30" stroke="#7aa2e3" stroke-width="2" />
        <rect :x="unitXs[i] - 37" :y="327 - (u.soc ?? 0) * 1.64" width="74" :height="(u.soc ?? 0) * 1.64" rx="12" fill="url(#tankfill)" opacity=".75" />
        <text :x="unitXs[i]" y="235" text-anchor="middle" class="big">{{ fixed(u.soc, 0) }}%</text>
        <text :x="unitXs[i]" y="254" text-anchor="middle" class="sv" fill="#ffd166">{{ u.mode }}</text>
        <text :x="unitXs[i]" y="352" text-anchor="middle" class="eq">{{ u.name }}</text>
      </g>
      <!-- 其他设备 -->
      <g v-else>
        <rect :x="unitXs[i] - 60" y="180" width="120" height="120" rx="6" :fill="u.type === 'hx' ? 'url(#tubes)' : '#13213b'"
              :stroke="u.running ? '#22c55e' : '#4a5873'" stroke-width="2" />
        <!-- 图标 -->
        <g :transform="`translate(${unitXs[i] + 40},198)`">
          <path v-if="u.type === 'boiler'" d="M0 -12 C6 -4 8 0 8 5 a8 8 0 0 1 -16 0 c0 -4 3 -7 5 -9 0 3 1 5 3 5 -1 -4 0 -7 0 -13z"
                :fill="u.running ? '#ff6b4a' : '#4a5873'" :class="{ blink: u.running && (u.load ?? 0) > 95 }" />
          <path v-else-if="u.type === 'eboiler'" d="M2 -12 L-6 2 H0 L-2 12 L7 -3 H1 Z" :fill="u.running ? '#facc15' : '#4a5873'" />
          <g v-else-if="u.type === 'heatpump'">
            <circle r="9" fill="none" :stroke="u.running ? '#2dd4bf' : '#4a5873'" stroke-width="2" />
            <path d="M-4 -4 L4 0 L-4 4 Z" :fill="u.running ? '#2dd4bf' : '#4a5873'" :class="{ spin: u.running }" />
          </g>
        </g>
        <rect :x="unitXs[i] - 56" y="212" width="112" height="84" rx="4" fill="#0b1426" opacity=".88" />
        <text :x="unitXs[i] - 52" y="200" class="un">{{ u.name }}</text>
        <text :x="unitXs[i] - 50" y="230" class="kv">负荷 <tspan class="sv" fill="#e6eefb">{{ fixed(u.load, 0) }}%</tspan></text>
        <text v-if="u.outlet !== undefined" :x="unitXs[i] - 50" y="248" class="kv">出水 <tspan class="sv" :fill="tempColor(u.outlet)">{{ fixed(u.outlet) }}℃</tspan></text>
        <text v-if="u.steam_tph !== undefined" :x="unitXs[i] - 50" y="266" class="kv">蒸汽 <tspan class="sv" fill="#facc15">{{ fixed(u.steam_tph) }}t/h</tspan></text>
        <text v-if="u.gas_nm3h !== undefined" :x="unitXs[i] - 50" y="266" class="kv">燃气 <tspan class="sv" fill="#facc15">{{ fixed(u.gas_nm3h, 0) }}Nm³/h</tspan></text>
        <text v-if="u.flue_t !== undefined" :x="unitXs[i] - 50" y="284" class="kv">排烟 <tspan class="sv">{{ fixed(u.flue_t, 0) }}℃</tspan> 效率 <tspan class="sv">{{ fixed(u.eff, 1) }}%</tspan></text>
        <text v-if="u.cop !== undefined" :x="unitXs[i] - 50" y="266" class="kv">COP <tspan class="sv" fill="#2dd4bf">{{ fixed(u.cop, 2) }}</tspan></text>
        <text v-if="u.power_kw !== undefined" :x="unitXs[i] - 50" y="266" class="kv">功率 <tspan class="sv" fill="#facc15">{{ fixed(u.power_kw, 0) }}kW</tspan></text>
        <!-- 负荷条 -->
        <rect :x="unitXs[i] - 50" y="290" width="100" height="4" fill="#22314d" />
        <rect :x="unitXs[i] - 50" y="290" :width="Math.min(100, u.load ?? 0)" height="4" :fill="(u.load ?? 0) > 90 ? '#ef4444' : '#22c55e'" />
      </g>
    </g>

    <!-- 补水 -->
    <rect x="40" y="350" width="60" height="78" rx="6" fill="#0d1a30" stroke="#4a6491" />
    <rect x="42" y="372" width="56" height="54" rx="4" fill="#1d4ed8" opacity=".6" />
    <text x="70" y="342" text-anchor="middle" class="eq">软化水箱</text>
    <path d="M70 428 V449" stroke="#38a8ff" class="pipe thin" />
    <DPump :x="70" :y="465" :running="d.makeup_tph > 0.1" />
    <path d="M70 481 V500 H110" stroke="#38a8ff" class="pipe thin flow-line" />

    <!-- 数据标签 -->
    <DTag :x="640" :y="20" label="供水温度" :value="fixed(d.ts1)" unit="℃" :color="cs" :w="128" />
    <DTag :x="774" :y="20" label="供水压力" :value="fixed(d.p_s, 3)" unit="MPa" :w="128" />
    <DTag :x="640" :y="530" label="回水温度" :value="fixed(d.tr1)" unit="℃" :color="cr" :w="128" />
    <DTag :x="774" :y="530" label="回水压力" :value="fixed(d.p_r, 3)" unit="MPa" :w="128" />
    <DTag :x="300" :y="20" label="循环流量" :value="fixed(d.g, 0)" unit="t/h" :w="128" />
    <DTag :x="434" :y="20" label="供热功率" :value="fixed(d.heat_mw, 2)" unit="MW" color="#ffd166" :w="128" />
    <DTag :x="300" :y="530" label="补水量" :value="fixed(d.makeup_tph, 2)" unit="t/h" :w="128" />
    <DTag :x="434" :y="530" label="负荷率" :value="fixed(d.load_pct)" unit="%" :w="128"
          :color="d.load_pct > 90 ? '#ef4444' : d.load_pct > 75 ? '#f5a524' : '#22c55e'" />
    <!-- 可视化高亮（viz_render twin） -->
    <g v-for="h in hlBoxes" :key="h.key" class="hl" :class="hlLevel">
      <rect :x="h.x" :y="h.y" :width="h.w" :height="h.h" rx="8" class="hl-box" />
      <rect :x="h.x" :y="h.y - 20" :width="h.label.length * 13 + 10" height="18" rx="3" class="hl-tag" />
      <text :x="h.x + 5" :y="h.y - 6" class="hl-lbl">{{ h.label }}</text>
    </g>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { SourceRT } from '@/api/types'
import { pipeColor, tempColor } from '@/utils/format'
import { fixed } from './svgParts'
import DTag from './DTag.vue'
import DPump from './DPump.vue'

const props = defineProps<{ d: SourceRT; highlight?: string[]; hlLevel?: string }>()
const pumps = computed(() => props.d.units.filter((u) => u.type === 'pump'))
const units = computed(() => props.d.units.filter((u) => u.type !== 'pump'))
const pumpXs = computed(() => pumps.value.map((_, i) => 136 + i * 78))
const unitXs = computed(() => {
  const n = units.value.length
  const x0 = 470
  const x1 = 900
  return units.value.map((_, i) => (n === 1 ? (x0 + x1) / 2 : x0 + ((x1 - x0) * i) / (n - 1)))
})
const hlBoxes = computed(() => {
  const xs = unitXs.value, ps = pumpXs.value
  const box: Record<string, { x: number; y: number; w: number; h: number; label: string }> = {
    energy: { x: 142, y: 104, w: 246, h: 52, label: '能源输入' },
    units: { x: (xs[0] ?? 470) - 72, y: 150, w: (xs[xs.length - 1] ?? 900) - (xs[0] ?? 470) + 144, h: 192, label: '热源机组' },
    pumps: { x: (ps[0] ?? 136) - 34, y: 404, w: (ps[ps.length - 1] ?? 136) - (ps[0] ?? 136) + 110, h: 66, label: '循环泵' },
    makeup: { x: 30, y: 326, w: 80, h: 166, label: '补水系统' },
  }
  return (props.highlight ?? []).filter((k) => box[k]).map((k) => ({ key: k, ...box[k] }))
})
const cs = computed(() => pipeColor('supply', props.d.ts1, 60, 120))
const cr = computed(() => pipeColor('return', props.d.tr1, 25, 60))
const stop = computed(() => (props.d.g < 1 ? 'stop' : ''))
const energy = computed(() => {
  const d = props.d
  if (d.kind === 'chp')
    return { title: '汽轮机抽汽', sub: `${fixed(d.steam_p, 2)}MPa · ${fixed(d.steam_t, 0)}℃ · ${fixed(d.steam_tph)}t/h`, color: '#facc15' }
  if (d.kind === 'gas') return { title: '天然气', sub: `${fixed(d.gas_nm3h, 0)} Nm³/h · 综合效率 ${fixed(d.eff)}%`, color: '#fbbf24' }
  return { title: '10kV 电源（峰谷电价）', sub: `用电 ${fixed((d.power_kw ?? 0) / 1000, 2)}MW · 热泵COP ${fixed(d.cop, 2)}`, color: '#a78bfa' }
})
</script>

<style scoped>
.diagram { width: 100%; height: auto; display: block; background: radial-gradient(circle at 50% 40%, #0f1c33, #0a1120 70%); border-radius: 6px; }
.pipe { fill: none; stroke-width: 8; stroke-linecap: round; stroke-linejoin: round; }
.pipe.thin { stroke-width: 5; }
.pipe.energy { stroke-width: 3; stroke-dasharray: 6 5; }
.pipe-lbl { font-size: 11px; fill: #8797b3; }
.en { font-size: 12px; font-weight: 700; }
.en-sub { font-size: 11px; fill: #b8c6dc; font-family: ui-monospace, monospace; }
.eq { font-size: 11px; fill: #b8c6dc; }
.un { font-size: 12px; fill: #e6eefb; font-weight: 600; }
.kv { font-size: 11px; fill: #8797b3; }
.sv { font-size: 11px; font-weight: 700; font-family: ui-monospace, monospace; }
.big { font-size: 20px; fill: #fff; font-weight: 700; font-family: ui-monospace, monospace; }
.hl-box { fill: rgba(59, 140, 255, .08); stroke: #3b8cff; stroke-width: 3; stroke-dasharray: 8 5; animation: viz-dash 1.2s linear infinite; }
.hl.warn .hl-box { stroke: #f5a524; fill: rgba(245, 165, 36, .08); }
.hl.danger .hl-box { stroke: #ef4444; fill: rgba(239, 68, 68, .1); }
.hl-lbl { font-size: 12px; font-weight: 700; fill: #fff; }
.hl-tag { fill: #3b8cff; }
.hl.warn .hl-tag { fill: #b7791f; }
.hl.danger .hl-tag { fill: #b91c1c; }
@keyframes viz-dash { to { stroke-dashoffset: -26; } }
</style>
