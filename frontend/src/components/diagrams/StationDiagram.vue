<template>
  <svg viewBox="0 0 1000 520" class="diagram" preserveAspectRatio="xMidYMid meet">
    <defs>
      <pattern id="plates" width="8" height="10" patternUnits="userSpaceOnUse">
        <rect width="8" height="10" fill="#14223b" />
        <line x1="4" y1="0" x2="4" y2="10" stroke="#5b7bb0" stroke-width="2" />
      </pattern>
      <linearGradient id="water" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="#38a8ff" stop-opacity=".9" />
        <stop offset="1" stop-color="#1d4ed8" stop-opacity=".9" />
      </linearGradient>
    </defs>

    <!-- 区域标注 -->
    <text x="20" y="28" class="zone">一次侧（一级管网）</text>
    <text x="520" y="28" class="zone">二次侧（小区管网）</text>
    <line x1="440" y1="40" x2="440" y2="510" stroke="#22314d" stroke-dasharray="4 6" />

    <!-- 一次供水 -->
    <path d="M20 110 H340 V150" :stroke="c.ts1" class="pipe flow-line" :class="flowCls(d.g1)" />
    <text x="20" y="98" class="pipe-lbl">一网供水 →</text>
    <!-- 一次回水 -->
    <path d="M340 370 V420 H20" :stroke="c.tr1" class="pipe flow-line" :class="flowCls(d.g1)" />
    <text x="20" y="410" class="pipe-lbl">← 一网回水</text>
    <!-- 热量表 -->
    <g transform="translate(200,420)">
      <rect x="-16" y="-12" width="32" height="24" rx="3" fill="#13213b" stroke="#7aa2e3" />
      <text y="4" text-anchor="middle" class="m">HM</text>
    </g>

    <DValve :x="190" :y="110" :opening="d.valve" label="电动调节阀" />

    <!-- 换热器 -->
    <rect x="320" y="150" width="80" height="220" rx="4" fill="url(#plates)" stroke="#7aa2e3" stroke-width="2" />
    <text x="408" y="248" class="eq">板式</text>
    <text x="408" y="263" class="eq">换热器</text>
    <text x="360" y="265" text-anchor="middle" class="q" transform="rotate(-90 360 265)">{{ fixed(d.heat_mw, 2) }} MW</text>

    <!-- 二次供水 -->
    <path d="M380 150 V110 H850" :stroke="c.ts2" class="pipe flow-line" :class="flowCls(d.g2)" />
    <text x="760" y="98" class="pipe-lbl">二网供水 →</text>
    <!-- 二次回水（两台泵并联） -->
    <path d="M850 420 H640" :stroke="c.tr2" class="pipe flow-line" :class="flowCls(d.g2)" />
    <path d="M640 420 H480" :stroke="c.tr2" class="pipe flow-line" :class="flowCls(d.g2)" />
    <path d="M640 420 V480 H480 V420" :stroke="c.tr2" class="pipe thin" />
    <path d="M480 420 H380 V370" :stroke="c.tr2" class="pipe flow-line" :class="flowCls(d.g2)" />
    <text x="760" y="410" class="pipe-lbl">← 二网回水</text>
    <DPump :x="560" :y="420" :running="d.online && d.g2 > 1" label="" />
    <DPump :x="560" :y="480" :running="false" label="" />
    <text x="590" y="400" class="eq">1#循环泵</text>
    <text x="590" y="510" class="eq muted">2#循环泵(备)</text>

    <!-- 补水系统 -->
    <rect x="690" y="240" width="70" height="90" rx="6" fill="#0d1a30" stroke="#4a6491" />
    <rect x="692" :y="330 - levelH" width="66" :height="levelH - 2" rx="4" fill="url(#water)" />
    <text x="725" y="232" text-anchor="middle" class="eq">软化水箱</text>
    <path d="M725 330 V352" stroke="#38a8ff" class="pipe thin" />
    <path d="M725 384 V420" stroke="#38a8ff" class="pipe thin flow-line" :class="flowCls(d.makeup_tph * 50)" />
    <DPump :x="725" :y="368" :running="d.makeup_tph > 0.05" />
    <text x="748" y="372" class="eq">补水泵</text>

    <!-- 建筑 -->
    <g transform="translate(855,140)">
      <rect x="0" y="40" width="40" height="230" fill="#15233e" stroke="#33507e" />
      <rect x="48" y="0" width="50" height="270" fill="#182a4a" stroke="#33507e" />
      <rect x="104" y="80" width="36" height="190" fill="#15233e" stroke="#33507e" />
      <g v-for="i in 9" :key="i">
        <rect :x="56" :y="12 + (i - 1) * 28" width="12" height="10" :fill="winColor" />
        <rect :x="76" :y="12 + (i - 1) * 28" width="12" height="10" :fill="winColor" />
      </g>
      <g v-for="i in 7" :key="'a' + i">
        <rect :x="8" :y="52 + (i - 1) * 30" width="10" height="9" :fill="winColor" />
        <rect :x="22" :y="52 + (i - 1) * 30" width="10" height="9" :fill="winColor" />
        <rect :x="112" :y="92 + (i - 1) * 25" width="10" height="9" :fill="winColor" v-if="i < 7" />
      </g>
    </g>
    <path d="M850 110 H870 V180" :stroke="c.ts2" class="pipe thin" />
    <path d="M870 410 V420 H850" :stroke="c.tr2" class="pipe thin" />

    <!-- 数据标签 -->
    <DTag :x="20" :y="124" label="供温" :value="fixed(d.ts1)" unit="℃" :color="c.ts1" />
    <DTag :x="20" :y="150" label="供压" :value="fixed(d.p1s, 3)" unit="MPa" />
    <DTag :x="20" :y="434" label="回温" :value="fixed(d.tr1)" unit="℃" :color="c.tr1" />
    <DTag :x="20" :y="460" label="回压" :value="fixed(d.p1r, 3)" unit="MPa" />
    <DTag :x="146" :y="452" label="流量" :value="fixed(d.g1)" unit="t/h" />
    <DTag :x="146" :y="478" label="热功率" :value="fixed(d.q1_mw, 2)" unit="MW" />

    <DTag :x="470" :y="54" label="二次供温" :value="fixed(d.ts2)" unit="℃" :color="c.ts2" />
    <DTag :x="594" :y="54" label="供压" :value="fixed(d.p2s, 3)" unit="MPa" />
    <DTag :x="718" :y="54" label="流量" :value="fixed(d.g2, 0)" unit="t/h" />
    <DTag :x="470" :y="124" label="供温设定" :value="fixed(d.ts2_set)" unit="℃" color="#f5a524" />
    <DTag :x="470" :y="150" label="控制模式" :value="MODE_TEXT[d.mode]" />
    <DTag :x="660" :y="434" label="二次回温" :value="fixed(d.tr2)" unit="℃" :color="c.tr2" />
    <DTag :x="660" :y="460" label="回压" :value="fixed(d.p2r, 3)" unit="MPa" />
    <DTag :x="470" :y="300" label="泵频率" :value="fixed(d.pump_freq)" unit="Hz" />
    <DTag :x="470" :y="326" label="泵功率" :value="fixed(d.pump_kw)" unit="kW" />
    <DTag :x="776" :y="250" :w="104" label="液位" :value="fixed(d.tank_level, 2)" unit="m" />
    <DTag :x="776" :y="276" :w="104" label="补水" :value="fixed(d.makeup_tph, 2)" unit="t/h"
          :color="d.makeup_tph > 0.5 ? '#ef4444' : undefined" />
    <DTag :x="852" :y="466" :w="140" label="典型室温" :value="fixed(d.tin)" unit="℃" :color="tinColor" />

    <!-- 可视化高亮（viz_render twin） -->
    <g v-for="h in hlBoxes" :key="h.key" class="hl" :class="hlLevel">
      <rect :x="h.x" :y="h.y" :width="h.w" :height="h.h" rx="8" class="hl-box" />
      <rect :x="h.x" :y="h.y - 20" :width="h.label.length * 13 + 10" height="18" rx="3" class="hl-tag" />
      <text :x="h.x + 5" :y="h.y - 6" class="hl-lbl">{{ h.label }}</text>
    </g>

    <g v-if="!d.online">
      <rect x="0" y="0" width="1000" height="520" fill="#0a1120" opacity=".55" />
      <text x="500" y="260" text-anchor="middle" class="offline blink">通讯中断 · 显示最后一次数据</text>
    </g>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StationRT } from '@/api/types'
import { MODE_TEXT, pipeColor } from '@/utils/format'
import { fixed } from './svgParts'
import DTag from './DTag.vue'
import DPump from './DPump.vue'
import DValve from './DValve.vue'

const props = defineProps<{ d: StationRT; highlight?: string[]; hlLevel?: string }>()
// 部件包围框（与上方 SVG 布局坐标一致）
const PARTS: Record<string, { x: number; y: number; w: number; h: number; label: string }> = {
  valve: { x: 156, y: 70, w: 70, h: 70, label: '电动调节阀' },
  hx: { x: 310, y: 140, w: 132, h: 240, label: '板式换热器' },
  heat_meter: { x: 178, y: 400, w: 44, h: 40, label: '热量表' },
  pump: { x: 528, y: 392, w: 120, h: 126, label: '循环泵' },
  makeup: { x: 680, y: 214, w: 96, h: 186, label: '补水系统' },
  building: { x: 848, y: 130, w: 150, h: 290, label: '用户楼栋' },
}
const hlBoxes = computed(() => (props.highlight ?? []).filter((k) => PARTS[k]).map((k) => ({ key: k, ...PARTS[k] })))
const c = computed(() => ({
  ts1: pipeColor('supply', props.d.ts1, 60, 120),
  tr1: pipeColor('return', props.d.tr1, 25, 60),
  ts2: pipeColor('supply', props.d.ts2, 28, 70),
  tr2: pipeColor('return', props.d.tr2, 22, 55),
}))
const levelH = computed(() => Math.max(6, Math.min(86, (props.d.tank_level / 2.5) * 86)))
const tinColor = computed(() => (props.d.tin < 18 ? '#ef4444' : props.d.tin > 22.5 ? '#f5a524' : '#22c55e'))
const winColor = computed(() => (props.d.tin < 18 ? '#3b5b8c' : props.d.tin > 22.5 ? '#ffb347' : '#f8d477'))
const flowCls = (g: number) => (!props.d.online || g < 0.5 ? 'stop' : '')
</script>

<style scoped>
.diagram { width: 100%; height: auto; display: block; background: radial-gradient(circle at 50% 40%, #0f1c33, #0a1120 70%); border-radius: 6px; }
.pipe { fill: none; stroke-width: 7; stroke-linecap: round; stroke-linejoin: round; }
.pipe.thin { stroke-width: 4; }
.zone { font-size: 13px; fill: #7f93b8; font-weight: 600; }
.pipe-lbl { font-size: 11px; fill: #8797b3; }
.eq { font-size: 11px; fill: #b8c6dc; }
.eq.muted { fill: #6b7790; }
.m { font-size: 10px; fill: #9fb4d8; font-weight: 700; }
.q { font-size: 13px; fill: #ffd166; font-weight: 700; font-family: ui-monospace, monospace; }
.offline { font-size: 26px; fill: #ef4444; font-weight: 700; }
.hl-box { fill: rgba(59, 140, 255, .08); stroke: #3b8cff; stroke-width: 3; stroke-dasharray: 8 5; animation: viz-dash 1.2s linear infinite; }
.hl.warn .hl-box { stroke: #f5a524; fill: rgba(245, 165, 36, .08); }
.hl.danger .hl-box { stroke: #ef4444; fill: rgba(239, 68, 68, .1); }
.hl-lbl { font-size: 12px; font-weight: 700; fill: #fff; }
.hl-tag { fill: #3b8cff; }
.hl.warn .hl-tag { fill: #b7791f; }
.hl.danger .hl-tag { fill: #b91c1c; }
@keyframes viz-dash { to { stroke-dashoffset: -26; } }
</style>
