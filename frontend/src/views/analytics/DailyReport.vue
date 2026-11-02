<template>
  <div class="wrap" v-loading="loading">
    <div class="panel bar">
      <el-date-picker v-model="day" type="date" value-format="YYYY-MM-DD" :clearable="false" size="small" :disabled-date="future" />
      <el-select v-model="scope" size="small" style="width: 200px" filterable>
        <el-option value="NET" label="全网" />
        <el-option-group label="热源">
          <el-option v-for="s in meta?.sources" :key="s.id" :value="s.id" :label="s.name" />
        </el-option-group>
        <el-option-group label="热力站">
          <el-option v-for="s in meta?.stations" :key="s.id" :value="s.id" :label="`${s.id} ${s.name}`" />
        </el-option-group>
      </el-select>
      <el-button size="small" @click="load">刷新</el-button>
      <el-button size="small" @click="exportCsv" :disabled="!r">导出 CSV</el-button>
      <span v-if="r?.partial" class="muted small">当日未结束，统计至最近整点</span>
    </div>

    <template v-if="r">
      <div class="grid kpis">
        <div v-for="k in kpis" :key="k.key" class="kpi">
          <div class="muted small">{{ k.label }}</div>
          <div class="num v">{{ fmt(r.compare[k.key]?.today, k.d) }}<span class="u">{{ k.unit }}</span></div>
          <div class="small">
            <span class="muted">日环比</span> <span :class="trendCls(r.compare[k.key]?.dod, k.good)">{{ pct(r.compare[k.key]?.dod, 1, true) }}</span>
            <span class="muted ml">同比</span> <span :class="trendCls(r.compare[k.key]?.yoy, k.good)">{{ pct(r.compare[k.key]?.yoy, 1, true) }}</span>
          </div>
        </div>
      </div>

      <div class="grid two">
        <div class="panel">
          <div class="panel-title">逐时热负荷与室外温度 <span class="sub">峰值 {{ r.peak.heat_mw }} MW @ {{ r.peak.time }}</span></div>
          <EChart :option="loadOpt" height="270px" />
        </div>
        <div class="panel">
          <div class="panel-title">逐时温度 <span class="sub">{{ r.scope_name }}</span></div>
          <EChart :option="tempOpt" height="270px" />
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">运行分析结论</div>
        <ul class="findings"><li v-for="f in r.findings" :key="f">{{ f }}</li></ul>
        <div class="grid sumgrid">
          <div class="kv"><span class="k">室外均温 / 度日数</span><span class="num">{{ fmt(r.summary.tout_avg) }}℃ / {{ fmt(r.summary.hdd) }} ℃·d</span></div>
          <div class="kv"><span class="k">面积热耗</span><span class="num">{{ fmt(r.summary.unit_heat, 2) }} GJ/万m²</span></div>
          <div class="kv"><span class="k">修正单耗</span><span class="num">{{ fmt(r.summary.norm_heat) }} kJ/(m²·℃·d)</span></div>
          <div class="kv"><span class="k">单位面积电耗</span><span class="num">{{ fmt(r.summary.unit_elec, 2) }} kWh/千m²</span></div>
          <div class="kv"><span class="k">单位面积水耗</span><span class="num">{{ fmt(r.summary.unit_water) }} kg/千m²</span></div>
          <div class="kv" v-if="r.summary.unit_cost"><span class="k">单位热成本</span><span class="num">{{ fmt(r.summary.unit_cost) }} 元/GJ</span></div>
          <div class="kv" v-if="r.summary.dt1"><span class="k">一次平均温差</span><span class="num">{{ fmt(r.summary.dt1) }} ℃</span></div>
          <div class="kv" v-if="r.summary.tin_avg"><span class="k">平均室温</span><span class="num">{{ fmt(r.summary.tin_avg) }} ℃</span></div>
        </div>
      </div>

      <div class="panel" v-if="r.rows.length">
        <div class="panel-title">分对象日报表</div>
        <el-table :data="r.rows" size="small" :default-sort="{ prop: 'norm_heat', order: 'descending' }" max-height="460">
          <el-table-column prop="name" label="对象" width="140" fixed />
          <el-table-column prop="heat_gj" label="热量 GJ" sortable align="right" />
          <el-table-column prop="heat_mw_max" label="最大负荷 MW" sortable align="right" width="116" />
          <el-table-column prop="unit_heat" label="GJ/万m²" sortable align="right" />
          <el-table-column prop="norm_heat" label="修正单耗" sortable align="right" />
          <el-table-column prop="elec_kwh" label="电量 kWh" sortable align="right" />
          <el-table-column prop="water_t" label="补水 t" sortable align="right" />
          <el-table-column prop="ts1_avg" label="一次供温" align="right" />
          <el-table-column prop="tr1_avg" label="一次回温" align="right" />
          <el-table-column prop="dt1" label="一次温差" sortable align="right" />
          <el-table-column prop="tin_avg" label="室温" sortable align="right">
            <template #default="{ row }"><span :class="row.tin_avg < 18.5 ? 'danger' : row.tin_avg > 22 ? 'warn' : ''">{{ row.tin_avg ?? '--' }}</span></template>
          </el-table-column>
          <el-table-column prop="tin_low_hours" label="低温时数" sortable align="right" />
          <el-table-column prop="unit_cost" label="元/GJ" sortable align="right" />
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import type { EChartsOption } from 'echarts'
import { ElMessage } from 'element-plus'
import EChart from '@/components/EChart.vue'
import { getData, qs } from '@/api/http'
import { useRealtime } from '@/stores/realtime'
import { fmt, pct, todayStr } from '@/utils/format'

const { meta } = storeToRefs(useRealtime())
const day = ref(todayStr(-1))
const scope = ref('NET')
const r = ref<any>(null)
const loading = ref(false)
const future = (d: Date) => d.getTime() > Date.now()

const kpis = [
  { key: 'heat_gj', label: '供热量', unit: 'GJ', d: 0, good: 'down' },
  { key: 'norm_heat', label: '度日数修正单耗', unit: 'kJ/m²·℃·d', d: 1, good: 'down' },
  { key: 'elec_kwh', label: '电量', unit: 'kWh', d: 0, good: 'down' },
  { key: 'water_t', label: '补水量', unit: 't', d: 1, good: 'down' },
  { key: 'tin_avg', label: '平均室温', unit: '℃', d: 2, good: 'none' },
  { key: 'tout_avg', label: '室外均温', unit: '℃', d: 1, good: 'none' },
]
const trendCls = (v: number | null, good: string) => {
  if (v === null || v === undefined || good === 'none') return 'muted'
  return (good === 'down' ? v <= 0 : v >= 0) ? 'ok' : 'danger'
}

async function load() {
  loading.value = true
  try {
    r.value = await getData(`/api/analytics/daily${qs({ day: day.value, scope: scope.value })}`)
  } catch (e: any) {
    ElMessage.error(e.message)
    r.value = null
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch([day, scope], load)

const loadOpt = computed<EChartsOption>(() => {
  const s = r.value?.series
  if (!s) return {}
  const extra = [] as any[]
  if (s.hp_mw) {
    extra.push({ name: '热泵', type: 'bar', stack: 'p', data: s.hp_mw, color: '#2dd4bf' })
    extra.push({ name: '电极锅炉', type: 'bar', stack: 'p', data: s.eb_mw, color: '#facc15' })
  }
  return {
    legend: {},
    xAxis: { type: 'category', data: s.ts },
    yAxis: [{ type: 'value', name: 'MW' }, { type: 'value', name: '℃', splitLine: { show: false } }],
    series: [
      { name: '热负荷', type: 'line', data: s.heat_mw, smooth: true, color: '#ff6b4a', areaStyle: { opacity: 0.12 } },
      ...extra,
      { name: '室外温度', type: 'line', yAxisIndex: 1, data: s.tout, smooth: true, color: '#38a8ff' },
    ],
  }
})
const tempOpt = computed<EChartsOption>(() => {
  const s = r.value?.series
  if (!s) return {}
  const ser: any[] = []
  const add = (k: string, n: string, c: string, dash = false) =>
    s[k] && ser.push({ name: n, type: 'line', data: s[k], smooth: true, showSymbol: false, color: c, lineStyle: dash ? { type: 'dashed' } : {} })
  add('ts1', '一次供温', '#ef4444')
  add('tr1', '一次回温', '#60a5fa')
  add('ts2', '二次供温', '#ff6b4a')
  add('ts2_set', '二次设定', '#f5a524', true)
  add('tr2', '二次回温', '#38a8ff')
  add('tin', '室温', '#22c55e')
  return { legend: {}, xAxis: { type: 'category', data: s.ts }, yAxis: { type: 'value', name: '℃', scale: true }, series: ser }
})

function exportCsv() {
  const rows = r.value.rows.length ? r.value.rows : [r.value.summary]
  const keys = Object.keys(rows[0])
  const csv = [keys.join(','), ...rows.map((x: any) => keys.map((k) => x[k] ?? '').join(','))].join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv' }))
  a.download = `日运行分析_${scope.value}_${day.value}.csv`
  a.click()
}
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; }
.kpis { grid-template-columns: repeat(6, minmax(0, 1fr)); }
.kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.v { font-size: 22px; margin: 4px 0; }
.u { font-size: 11px; color: var(--muted); margin-left: 4px; }
.small { font-size: 12px; }
.ml { margin-left: 8px; }
.two { grid-template-columns: 1fr 1fr; }
.findings { margin: 0 0 10px; padding-left: 18px; line-height: 1.9; }
.sumgrid { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0 24px; }
@media (max-width: 1200px) { .kpis { grid-template-columns: repeat(3, 1fr); } .two { grid-template-columns: 1fr; } .sumgrid { grid-template-columns: 1fr 1fr; } }
</style>
