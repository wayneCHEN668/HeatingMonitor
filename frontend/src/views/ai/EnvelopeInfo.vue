<template>
  <div class="env" v-if="env">
    <div class="sum"><el-icon class="ic"><MagicStick /></el-icon>{{ env.summary }}</div>
    <div class="meta">
      <span v-if="env.confidence !== null && env.confidence !== undefined" class="conf">置信度 {{ (env.confidence * 100).toFixed(0) }}%</span>
      <span v-for="e in env.evidence" :key="e" class="ev">依据：{{ e }}</span>
      <span v-for="w in env.warnings" :key="w" class="wn">⚠ {{ w }}</span>
      <span class="muted tid">trace {{ env.trace_id }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { MagicStick } from '@element-plus/icons-vue'
import type { Envelope } from '@/api/types'

defineProps<{ env: Envelope<any> | null }>()
</script>

<style scoped>
.env { background: linear-gradient(90deg, #102544, #0f1a2e); border: 1px solid #24406d; border-radius: 8px; padding: 10px 14px; }
.sum { font-size: 15px; font-weight: 600; color: #e8f0fc; display: flex; gap: 8px; align-items: center; }
.ic { color: #a78bfa; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 6px; font-size: 12px; }
.conf { color: #a78bfa; }
.ev { color: #9fb4d8; }
.wn { color: var(--warn); }
.tid { font-family: ui-monospace, monospace; }
</style>
