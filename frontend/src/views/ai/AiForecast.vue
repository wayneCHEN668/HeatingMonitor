<template>
  <div class="wrap" v-loading="loading">
    <div class="panel bar">
      <span class="muted">预测对象</span>
      <el-select v-model="sc" size="small" style="width: 200px" filterable>
        <el-option value="NET" label="全网" />
        <el-option-group label="热源"><el-option v-for="s in meta?.sources" :key="s.id" :value="s.id" :label="s.name" /></el-option-group>
        <el-option-group label="热力站"><el-option v-for="s in meta?.stations" :key="s.id" :value="s.id" :label="`${s.id} ${s.name}`" /></el-option-group>
      </el-select>
      <span class="muted">预见期</span>
      <el-radio-group v-model="horizon" size="small">
        <el-radio-button :value="24">24h</el-radio-button>
        <el-radio-button :value="72">72h</el-radio-button>
        <el-radio-button :value="168">7 天</el-radio-button>
      </el-radio-group>
      <span v-if="d" class="muted small">{{ d.model }} · 训练 {{ d.train_ms }}ms</span>
    </div>
    <EnvelopeInfo :env="env" />
    <template v-if="d">
      <div class="panel">
        <div class="panel-title">热负荷预测 <span class="sub">实线为实测，虚线为 P50，阴影为 P10–P90 区间；右轴为室外温度（预报）</span></div>
        <EChart :option="mainOpt" height="340px" not-merge />
      </div>
      <div class="grid three">
        <div class="panel">
          <div class="panel-title">回测（最近 48h 留出） <span class="sub">MAPE {{ d.backtest.mape }}% · 覆盖率 {{ d.backtest.coverage }}%</span></div>
          <EChart :option="btOpt" height="240px" not-merge />
          <div class="muted small">区间 = P50 ×（1 + 样本外相对残差分位数 {{ d.backtest.rel_q10 }}% / +{{ d.backtest.rel_q90 }}%），残差来自时序交叉验证；覆盖率为留出集上的样本外结果（名义 80%）。</div>
        </div>
        <div class="panel">
          <div class="panel-title">主要驱动因素 <span class="sub">特征重要性</span></div>
          <EChart :option="drvOpt" height="240px" not-merge />
        </div>
        <div class="panel">
          <div class="panel-title">逐日供热量预测</div>
          <EChart :option="dailyOpt" height="240px" not-merge />
          <div class="kv"><span class="k">预测峰值</span><span class="num">{{ d.peak.p50 }} MW（P90 {{ d.peak.p90 }}）@ {{ d.peak.ts }}</span></div>
          <div class="kv" v-if="d.capacity_mw"><span class="k">装机容量</span><span class="num">{{ d.capacity_mw }} MW · 峰值负荷率 {{ fmt((d.peak.p90 / d.capacity_mw) * 100) }}%</span></div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import type { EChartsOption } from 'echarts'
import { ElMessage } from 'element-plus'
import EChart from '@/components/EChart.vue'
import EnvelopeInfo from './EnvelopeInfo.vue'
import { api, qs } from '@/api/http'
import type { Envelope } from '@/api/types'
import { useRealtime } from '@/stores/realtime'
import { fmt } from '@/utils/format'

const props = defineProps<{ scope: string }>()
const { meta } = storeToRefs(useRealtime())
const sc = ref(props.scope)
const horizon = ref(72)
const env = ref<Envelope<any> | null>(null)
const d = computed(() => env.value?.data)
const loading = ref(false)
watch(() => props.scope, (v) => (sc.value = v))

async function load() {
  loading.value = true
  try {
    env.value = await api.get(`/api/ai/forecast${qs({ scope: sc.value, horizon: horizon.value })}`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
watch([sc, horizon], load, { immediate: true })

function band(ts: string[], p10: number[], p90: number[], pad: number) {
  const nul = Array(pad).fill(null)
  return [
    { name: 'P10–P90', type: 'line', stack: 'b', data: [...nul, ...p10], lineStyle: { opacity: 0 }, showSymbol: false, color: '#3b8cff', tooltip: { show: false } },
    { name: 'P10–P90', type: 'line', stack: 'b', data: [...nul, ...p90.map((v, i) => +(v - p10[i]).toFixed(3))], lineStyle: { opacity: 0 },
      showSymbol: false, areaStyle: { color: 'rgba(59,140,255,.22)' }, color: '#3b8cff', tooltip: { show: false } },
  ]
}

const mainOpt = computed<EChartsOption>(() => {
  const x = d.value
  if (!x) return {}
  const ts = [...x.history.ts, ...x.forecast.ts]
  const n = x.history.ts.length
  const nul = (k: number) => Array(k).fill(null)
  return {
    legend: { data: ['实测负荷', 'P50 预测', 'P10–P90', '室外温度'] },
    tooltip: { trigger: 'axis' },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 4 }],
    grid: { bottom: 50 },
    xAxis: { type: 'category', data: ts, axisLabel: { interval: 11 } },
    yAxis: [{ type: 'value', name: 'MW', scale: true }, { type: 'value', name: '℃', splitLine: { show: false } }],
    series: [
      { name: '实测负荷', type: 'line', data: x.history.heat_mw, showSymbol: false, color: '#ff6b4a', lineStyle: { width: 2 } },
      ...(band(ts, x.forecast.p10, x.forecast.p90, n) as any),
      { name: 'P50 预测', type: 'line', data: [...nul(n), ...x.forecast.p50], showSymbol: false, color: '#3b8cff', lineStyle: { type: 'dashed', width: 2 },
        markLine: x.capacity_mw ? { symbol: 'none', data: [{ yAxis: x.capacity_mw }], label: { formatter: '装机 {c}MW' }, lineStyle: { color: '#ef4444' } } : undefined },
      { name: '室外温度', type: 'line', yAxisIndex: 1, data: [...x.history.tout, ...x.forecast.tout], showSymbol: false, color: '#94a3b8', lineStyle: { width: 1 },
        markArea: { silent: true, itemStyle: { color: 'rgba(148,163,184,.06)' }, data: [[{ xAxis: x.forecast.ts[0] }, { xAxis: x.forecast.ts[x.forecast.ts.length - 1] }]] } },
    ],
  }
})
const btOpt = computed<EChartsOption>(() => {
  const b = d.value?.backtest
  if (!b) return {}
  return {
    legend: { data: ['实测', 'P50', 'P10–P90'] },
    xAxis: { type: 'category', data: b.ts, axisLabel: { interval: 11 } },
    yAxis: { type: 'value', scale: true, name: 'MW' },
    series: [
      ...(band(b.ts, b.p10, b.p90, 0) as any),
      { name: '实测', type: 'line', data: b.actual, showSymbol: false, color: '#ff6b4a' },
      { name: 'P50', type: 'line', data: b.p50, showSymbol: false, color: '#3b8cff', lineStyle: { type: 'dashed' } },
    ],
  }
})
const drvOpt = computed<EChartsOption>(() => {
  const dr = [...(d.value?.drivers ?? [])].reverse()
  return {
    grid: { left: 140, right: 40, top: 10, bottom: 20 },
    xAxis: { type: 'value', max: 100, name: '%' },
    yAxis: { type: 'category', data: dr.map((x: any) => x.name) },
    series: [{ type: 'bar', data: dr.map((x: any) => x.weight), color: '#a78bfa', barWidth: 12, label: { show: true, position: 'right', color: '#8797b3', formatter: '{c}%' } }],
  }
})
const dailyOpt = computed<EChartsOption>(() => {
  const dd = d.value?.daily_gj ?? []
  return {
    grid: { left: 50, right: 10, top: 20, bottom: 24 },
    xAxis: { type: 'category', data: dd.map((x: any) => x.date) },
    yAxis: { type: 'value', name: 'GJ' },
    series: [{ type: 'bar', data: dd.map((x: any) => x.heat_gj), color: '#f5a524', barMaxWidth: 34, label: { show: true, position: 'top', color: '#8797b3' } }],
  }
})
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; }
.small { font-size: 12px; }
.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
@media (max-width: 1300px) { .three { grid-template-columns: 1fr; } }
</style>
