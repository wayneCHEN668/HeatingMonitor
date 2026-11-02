<template>
  <div class="wrap" v-loading="loading">
    <div class="panel bar">
      <span class="muted">指标</span>
      <el-select v-model="metric" size="small" style="width: 150px">
        <el-option v-for="(v, k) in METRICS" :key="k" :value="k" :label="v" />
      </el-select>
      <span class="muted">对象维度</span>
      <el-radio-group v-model="level" size="small">
        <el-radio-button value="source">热源</el-radio-button>
        <el-radio-button value="station">热力站</el-radio-button>
      </el-radio-group>
      <span class="muted">时间维度</span>
      <el-radio-group v-model="grain" size="small">
        <el-radio-button value="day">按日</el-radio-button>
        <el-radio-button value="hour">按小时（日内规律）</el-radio-button>
        <el-radio-button value="weekday">按星期</el-radio-button>
      </el-radio-group>
      <span class="muted">范围</span>
      <el-select v-model="days" size="small" style="width: 100px">
        <el-option v-for="d in [3, 7, 14]" :key="d" :value="d" :label="`近 ${d} 天`" />
      </el-select>
    </div>

    <template v-if="r">
      <div class="panel">
        <div class="panel-title">
          {{ r.metric_name }}热力图 <span class="sub">{{ levelText }} × {{ grainText }}，单元格为{{ r.agg }}（{{ r.unit }}）</span>
        </div>
        <EChart :option="heatOpt" :height="`${Math.max(240, r.rows.length * 30 + 80)}px`" not-merge />
      </div>
      <div class="grid two">
        <div class="panel">
          <div class="panel-title">按{{ levelText }}汇总</div>
          <EChart :option="barOpt" height="300px" not-merge />
        </div>
        <div class="panel">
          <div class="panel-title">{{ isSum ? '占比结构' : '时间维度曲线' }}</div>
          <EChart :option="isSum ? pieOpt : lineOpt" height="300px" not-merge />
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">透视表 <el-button size="small" @click="exportCsv">导出 CSV</el-button></div>
        <el-table :data="tableRows" size="small" max-height="420">
          <el-table-column prop="name" label="对象" width="140" fixed />
          <el-table-column v-for="(l, i) in r.labels" :key="l" :label="l" align="right" min-width="72">
            <template #default="{ row }">{{ fmt(row.values[i], dig) }}</template>
          </el-table-column>
          <el-table-column label="合计/均值" align="right" fixed="right" width="100">
            <template #default="{ row }"><b>{{ fmt(row.total, dig) }}</b></template>
          </el-table-column>
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { ElMessage } from 'element-plus'
import EChart from '@/components/EChart.vue'
import { getData, qs } from '@/api/http'
import { fmt } from '@/utils/format'

const METRICS: Record<string, string> = {
  heat_gj: '热量', norm_heat: '度日数修正单耗', unit_heat: '面积热耗', elec_kwh: '电量', water_t: '补水量', tin: '平均室温', cost_yuan: '运行成本',
}
const metric = ref('heat_gj')
const level = ref<'source' | 'station'>('station')
const grain = ref<'day' | 'hour' | 'weekday'>('day')
const days = ref(7)
const r = ref<any>(null)
const loading = ref(false)

const levelText = computed(() => (r.value?.level === 'source' ? '热源' : '热力站'))
const grainText = computed(() => ({ day: '日期', hour: '小时', weekday: '星期' })[grain.value])
const isSum = computed(() => ['heat_gj', 'elec_kwh', 'water_t', 'cost_yuan'].includes(metric.value))
const dig = computed(() => (['elec_kwh', 'cost_yuan'].includes(metric.value) ? 0 : metric.value === 'tin' ? 2 : 1))
const tableRows = computed(() => r.value?.rows ?? [])

async function load() {
  loading.value = true
  try {
    r.value = await getData(`/api/analytics/multidim${qs({ metric: metric.value, level: level.value, grain: grain.value, days: days.value })}`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
watch([metric, level, grain, days], load, { immediate: true })

const heatOpt = computed<EChartsOption>(() => {
  const x = r.value
  if (!x) return {}
  const data: any[] = []
  let lo = Infinity
  let hi = -Infinity
  x.rows.forEach((row: any, i: number) =>
    row.values.forEach((v: number | null, j: number) => {
      if (v === null) return
      data.push([j, i, v])
      lo = Math.min(lo, v)
      hi = Math.max(hi, v)
    }),
  )
  return {
    tooltip: { trigger: 'item', formatter: (p: any) => `${x.rows[p.data[1]].name} · ${x.labels[p.data[0]]}<br/><b>${fmt(p.data[2], dig.value)}</b> ${x.unit}` },
    grid: { left: 90, right: 80, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: x.labels, splitArea: { show: true } },
    yAxis: { type: 'category', data: x.rows.map((r: any) => r.name), splitArea: { show: true } },
    visualMap: {
      min: lo, max: hi, calculable: true, orient: 'vertical', right: 0, top: 'center', itemHeight: 140,
      textStyle: { color: '#8797b3' },
      inRange: { color: metric.value === 'tin' ? ['#1d4ed8', '#22c55e', '#f59e0b', '#ef4444'] : ['#0f2a4a', '#1d4ed8', '#f59e0b', '#ef4444'] },
    },
    series: [{ type: 'heatmap', data, label: { show: x.labels.length <= 14, color: '#fff', fontSize: 10, formatter: (p: any) => fmt(p.data[2], dig.value > 1 ? 1 : dig.value) } }],
  }
})
const barOpt = computed<EChartsOption>(() => {
  const x = r.value
  if (!x) return {}
  const rows = [...x.rows].sort((a: any, b: any) => a.total - b.total)
  return {
    grid: { left: 90, right: 50, top: 10, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', scale: !isSum.value },
    yAxis: { type: 'category', data: rows.map((r: any) => r.name) },
    series: [{ type: 'bar', barWidth: 12, data: rows.map((r: any) => r.total), color: '#3b8cff',
               label: { show: true, position: 'right', color: '#8797b3', formatter: (p: any) => fmt(p.value, dig.value) } }],
  }
})
const pieOpt = computed<EChartsOption>(() => {
  const x = r.value
  if (!x) return {}
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { show: false },
    series: [{ type: 'pie', radius: ['40%', '72%'], itemStyle: { borderColor: '#0f1a2e', borderWidth: 2 },
               label: { color: '#b8c6dc', formatter: '{b}\n{d}%' },
               data: x.rows.map((r: any) => ({ name: r.name, value: +Number(r.total).toFixed(1) })) }],
  }
})
const lineOpt = computed<EChartsOption>(() => {
  const x = r.value
  if (!x) return {}
  return {
    legend: { type: 'scroll' },
    xAxis: { type: 'category', data: x.labels },
    yAxis: { type: 'value', scale: true, name: x.unit },
    series: x.rows.map((r: any) => ({ name: r.name, type: 'line', data: r.values, showSymbol: false, smooth: true })),
  }
})

function exportCsv() {
  const x = r.value
  const lines = [['对象', ...x.labels, '合计/均值'].join(','), ...x.rows.map((r: any) => [r.name, ...r.values, r.total].join(','))]
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob(['﻿' + lines.join('\n')], { type: 'text/csv' }))
  a.download = `多维统计_${x.metric_name}_${grain.value}.csv`
  a.click()
}
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; }
.two { grid-template-columns: 1fr 1fr; }
@media (max-width: 1200px) { .two { grid-template-columns: 1fr; } }
</style>
