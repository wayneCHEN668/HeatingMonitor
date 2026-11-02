<template>
  <div class="wrap" v-loading="loading">
    <div class="panel bar">
      <span class="muted">已调整站点</span>
      <el-radio-group v-model="sel" size="small">
        <el-radio-button v-for="s in ['ST01', 'ST04', 'ST05']" :key="s" :value="s">{{ rt.stationMeta[s]?.name }}</el-radio-button>
      </el-radio-group>
      <span class="muted small">这些站已在本季采纳过优化曲线；对照组为同期未调整且无异常的站点。</span>
    </div>
    <EnvelopeInfo :env="env" />
    <template v-if="d">
      <div class="grid kpis">
        <KpiCard label="处理站修正单耗变化" :value="pct(d.treat.change_pct, 1, true)" :sub="`${d.treat.pre} → ${d.treat.post}`" />
        <KpiCard label="对照组变化" :value="pct(d.control.change_pct, 1, true)" :sub="`${d.control.ids.length} 个站 · ${d.control.pre} → ${d.control.post}`" />
        <KpiCard label="净节能效果（DiD）" :value="pct(d.did_pct, 1, true)" :tone="d.did_pct < 0 ? 'ok' : 'danger'" sub="负值为节能" />
        <KpiCard label="室温净变化" :value="(d.tin_did > 0 ? '+' : '') + d.tin_did" unit="℃" :sub="`${d.treat.tin_pre} → ${d.treat.tin_post}℃`" />
        <KpiCard label="调整时间" :value="d.switch" />
      </div>
      <div class="panel">
        <div class="panel-title">逐日修正单耗：处理站 vs 对照组 <span class="sub">{{ d.method }}</span></div>
        <EChart :option="opt" height="320px" not-merge />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import KpiCard from '@/components/KpiCard.vue'
import EnvelopeInfo from './EnvelopeInfo.vue'
import { api } from '@/api/http'
import type { Envelope } from '@/api/types'
import { useRealtime } from '@/stores/realtime'
import { pct } from '@/utils/format'

const rt = useRealtime()
const sel = ref('ST01')
const env = ref<Envelope<any> | null>(null)
const d = computed(() => env.value?.data)
const loading = ref(false)
watch(sel, async (s) => {
  loading.value = true
  try {
    env.value = await api.get(`/api/ai/effect/${s}`)
  } finally {
    loading.value = false
  }
}, { immediate: true })

const opt = computed<EChartsOption>(() => {
  const x = d.value
  if (!x) return {}
  const sw = x.switch.slice(0, 5)
  return {
    legend: {},
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: x.series.map((s: any) => s.date) },
    yAxis: [{ type: 'value', name: 'kJ/(m²·℃·d)', scale: true }, { type: 'value', name: '室温℃', scale: true, splitLine: { show: false } }],
    series: [
      { name: x.station_name, type: 'line', data: x.series.map((s: any) => s.treat), color: '#22c55e', lineStyle: { width: 3 },
        markLine: { symbol: 'none', data: [{ xAxis: sw }], label: { formatter: '曲线调整' }, lineStyle: { color: '#f5a524' } } },
      { name: '对照组均值', type: 'line', data: x.series.map((s: any) => s.control), color: '#94a3b8', lineStyle: { type: 'dashed' } },
      { name: '室温', type: 'bar', yAxisIndex: 1, data: x.series.map((s: any) => s.tin), color: 'rgba(59,140,255,.35)', barMaxWidth: 24 },
    ],
  }
})
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; }
.small { font-size: 12px; }
.kpis { grid-template-columns: repeat(5, minmax(0, 1fr)); }
@media (max-width: 1100px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
</style>
