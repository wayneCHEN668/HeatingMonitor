<template>
  <g :transform="`translate(${x},${y})`">
    <!-- 执行机构 -->
    <line x1="0" y1="0" x2="0" y2="-20" stroke="#8797b3" stroke-width="2" />
    <rect x="-13" y="-34" width="26" height="14" rx="2" fill="#1b2a48" stroke="#4a6491" />
    <text y="-24" text-anchor="middle" class="m">M</text>
    <!-- 阀体 -->
    <path d="M-14 -9 L0 0 L-14 9 Z M14 -9 L0 0 L14 9 Z" :fill="fill" stroke="#cfd8e8" stroke-width="1" />
    <text y="26" text-anchor="middle" class="v">{{ label }}</text>
    <text y="40" text-anchor="middle" class="o" :fill="fill">{{ opening.toFixed(1) }}%</text>
  </g>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ x: number; y: number; opening: number; label?: string }>()
const fill = computed(() => (props.opening > 98 ? '#ef4444' : props.opening < 8 ? '#94a3b8' : '#f5a524'))
</script>

<style scoped>
.m { font-size: 10px; fill: #9fb4d8; font-weight: 700; }
.v { font-size: 11px; fill: #b8c6dc; }
.o { font-size: 12px; font-weight: 700; font-family: ui-monospace, monospace; }
</style>
