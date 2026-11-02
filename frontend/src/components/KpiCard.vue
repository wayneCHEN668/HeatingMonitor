<template>
  <div class="kpi" :class="tone">
    <div class="label">{{ label }}</div>
    <div class="value num">
      {{ value }}<span class="unit">{{ unit }}</span>
    </div>
    <div v-if="sub || $slots.sub" class="sub"><slot name="sub">{{ sub }}</slot></div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ label: string; value: string | number; unit?: string; sub?: string; tone?: string }>()
</script>

<style scoped>
.kpi { background: linear-gradient(160deg, #13213b, #0f1a2e); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; min-width: 0; }
.label { color: var(--muted); font-size: 12px; }
.value { font-size: 24px; font-weight: 600; margin-top: 6px; color: #f1f6ff; white-space: nowrap; }
.unit { font-size: 12px; color: var(--muted); margin-left: 4px; font-weight: 400; }
.sub { font-size: 12px; color: var(--muted); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi.supply .value { color: var(--supply); }
.kpi.return .value { color: var(--return); }
.kpi.ok .value { color: var(--ok); }
.kpi.warn .value { color: var(--warn); }
.kpi.danger .value { color: var(--danger); }
</style>
