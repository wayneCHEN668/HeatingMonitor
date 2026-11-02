<template>
  <g :transform="`translate(${x},${y})`">
    <circle r="16" :fill="running ? '#0f2a1c' : '#1a2235'" :stroke="running ? '#22c55e' : '#5b6780'" stroke-width="2" />
    <g :class="{ spin: running }">
      <path d="M0 -10 L9 5 L-9 5 Z" :fill="running ? '#22c55e' : '#5b6780'" />
    </g>
    <text v-if="label" y="32" text-anchor="middle" class="pl">{{ label }}</text>
    <text v-if="sub" y="45" text-anchor="middle" class="ps" :fill="running ? '#7ee2a8' : '#6b7790'">{{ sub }}</text>
  </g>
</template>

<script setup lang="ts">
defineProps<{ x: number; y: number; running: boolean; label?: string; sub?: string }>()
</script>

<style scoped>
.pl { font-size: 11px; fill: #b8c6dc; }
.ps { font-size: 10px; font-family: ui-monospace, monospace; }
</style>
