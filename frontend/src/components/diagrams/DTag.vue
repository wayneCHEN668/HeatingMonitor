<template>
  <g :transform="`translate(${x},${y})`" class="dtag">
    <rect :width="w" height="22" rx="3" fill="#0b1426" :stroke="color || '#2c3f63'" stroke-width="1" />
    <text x="6" y="15" class="lbl">{{ label }}</text>
    <text :x="w - 6" y="15" text-anchor="end" class="val" :fill="color || '#e6eefb'">
      {{ value }}<tspan class="unit" dx="2">{{ unit }}</tspan>
    </text>
  </g>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ x: number; y: number; label: string; value: string; unit?: string; color?: string; w?: number }>(), {
  w: 118,
  unit: '',
  color: undefined,
})
</script>

<style scoped>
.lbl { font-size: 11px; fill: #8797b3; }
.val { font-size: 12px; font-weight: 600; font-family: 'Roboto Mono', ui-monospace, monospace; }
.unit { font-size: 10px; fill: #8797b3; font-weight: 400; }
</style>
