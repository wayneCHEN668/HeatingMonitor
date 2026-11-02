<template>
  <div class="wrap" v-loading="!env">
    <EnvelopeInfo :env="env" />
    <div v-if="d" class="grid main">
      <div class="panel">
        <div class="panel-title">异常事件 <span class="sub">{{ d.model }}</span></div>
        <el-empty v-if="!d.events.length" description="未发现持续性异常" />
        <div v-for="e in d.events" :key="e.station_id" class="ev" :class="{ active: sel === e.station_id, known: e.known_cause }" @click="sel = e.station_id">
          <div class="ev-head">
            <el-tag size="small" :type="e.severity === '高' ? 'danger' : e.severity === '中' ? 'warning' : 'info'" effect="dark">{{ e.severity }}</el-tag>
            <b>{{ e.station_name }}</b>
            <span>{{ e.type }}</span>
          </div>
          <ul class="evid"><li v-for="x in e.evidence" :key="x">{{ x }}</li></ul>
          <div class="adv">处置建议：{{ e.advice }}</div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">
          {{ selName }} 特征时序
          <el-select v-model="sel" size="small" style="width: 150px">
            <el-option v-for="s in meta?.stations" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
        </div>
        <EChart :option="seriesOpt" height="420px" not-merge />
        <div v-if="selEvent" class="z">
          <span class="muted">偏离基线程度 |z|：</span>
          <span v-for="(v, k) in selEvent.zscores" :key="k" class="zi" :class="{ hi: v > 3 }">{{ d.features[k] }} {{ v }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import EnvelopeInfo from './EnvelopeInfo.vue'
import { api, getData } from '@/api/http'
import type { Envelope } from '@/api/types'
import { useRealtime } from '@/stores/realtime'

const props = defineProps<{ station: string }>()
const rt = useRealtime()
const { meta } = storeToRefs(rt)
const env = ref<Envelope<any> | null>(null)
const d = computed(() => env.value?.data)
const sel = ref(props.station)
const series = ref<any>(null)
const selName = computed(() => rt.stationMeta[sel.value]?.name ?? '')
const selEvent = computed(() => d.value?.events.find((e: any) => e.station_id === sel.value))

onMounted(async () => {
  env.value = await api.get('/api/ai/anomalies?days=7')
  if (!sel.value) sel.value = env.value.data.events[0]?.station_id ?? 'ST01'
})
watch(sel, async (s) => {
  if (s) series.value = await getData(`/api/ai/anomalies/${s}/series?days=10`)
}, { immediate: true })

const seriesOpt = computed<EChartsOption>(() => {
  const s = series.value
  if (!s) return {}
  const ev = selEvent.value
  const panels = [
    { k: 'heat_balance', n: '一/二次热量偏差 %', c: '#f5a524' },
    { k: 'makeup', n: '单位面积补水 t/(万m²·h)', c: '#38a8ff' },
    { k: 'tin', n: '室温 ℃', c: '#22c55e' },
    { k: 'dt1', n: '一次温差 ℃', c: '#ff6b4a' },
  ]
  const h = 100 / panels.length
  return {
    tooltip: { trigger: 'axis' },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: panels.map((_, i) => ({ left: 60, right: 20, top: `${i * h + 5}%`, height: `${h - 10}%` })),
    xAxis: panels.map((_, i) => ({ type: 'category', data: s.ts, gridIndex: i, axisLabel: { show: i === panels.length - 1, interval: 23 } })),
    yAxis: panels.map((p, i) => ({ type: 'value', gridIndex: i, scale: true, name: p.n, nameTextStyle: { align: 'left' }, splitNumber: 2 })),
    series: panels.map((p, i) => ({
      name: p.n, type: 'line', data: s[p.k], xAxisIndex: i, yAxisIndex: i, showSymbol: false, color: p.c,
      markArea: ev && ev.feature === p.k
        ? { itemStyle: { color: 'rgba(239,68,68,.12)' }, data: [[{ xAxis: ev.start }, { xAxis: s.ts[s.ts.length - 1] }]] }
        : undefined,
    })),
  }
})
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; min-height: 300px; }
.main { grid-template-columns: 420px minmax(0, 1fr); }
.ev { border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; margin-bottom: 8px; cursor: pointer; }
.ev.active { border-color: #3b8cff; background: #0f2140; }
.ev.known { opacity: .7; }
.ev-head { display: flex; gap: 8px; align-items: center; }
.evid { margin: 6px 0; padding-left: 18px; font-size: 12px; color: #9fb4d8; line-height: 1.7; }
.adv { font-size: 12px; color: #c7d6ee; }
.z { font-size: 12px; display: flex; flex-wrap: wrap; gap: 6px 12px; }
.zi { color: var(--muted); }
.zi.hi { color: var(--danger); font-weight: 700; }
@media (max-width: 1200px) { .main { grid-template-columns: 1fr; } }
</style>
