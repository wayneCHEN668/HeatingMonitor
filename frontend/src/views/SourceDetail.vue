<template>
  <div class="page" v-if="h && meta">
    <div class="row head">
      <el-radio-group :model-value="id" @update:model-value="(v: any) => router.replace(`/sources/${v}`)" size="small">
        <el-radio-button v-for="s in meta.sources" :key="s.id" :value="s.id">{{ s.name }}</el-radio-button>
      </el-radio-group>
      <span class="muted">{{ KIND_TEXT[h.kind] }} · 装机 {{ h.capacity_mw }} MW · 带 {{ served.length }} 个热力站</span>
    </div>

    <div class="grid top">
      <div class="panel">
        <div class="panel-title">工艺流程图 <span class="sub">实时 · {{ snap!.ts.slice(11) }}</span></div>
        <SourceDiagram :d="h" />
      </div>
      <div class="side">
        <div class="grid k2">
          <KpiCard label="今日供热量" :value="fmtInt(today.heat_gj)" unit="GJ" />
          <KpiCard label="今日电耗" :value="fmtInt(today.elec_kwh)" unit="kWh" />
          <KpiCard v-if="h.kind === 'gas'" label="今日燃气" :value="fmtInt(today.gas_nm3)" unit="Nm³" />
          <KpiCard v-else-if="h.kind === 'chp'" label="今日抽汽" :value="fmtInt(today.steam_t)" unit="t" />
          <KpiCard v-else label="蓄热罐" :value="fmt(h.tank_soc, 0)" unit="%" />
          <KpiCard label="今日成本" :value="fmt((today.cost_yuan ?? 0) / 1e4, 2)" unit="万元"
                   :sub="`${fmt((today.cost_yuan ?? 0) / Math.max(today.heat_gj ?? 1, 1))} 元/GJ`" />
        </div>
        <div class="panel">
          <div class="panel-title">所带热力站</div>
          <el-table :data="served" size="small" @row-click="(r: any) => router.push(`/stations/${r.id}`)" class="clickable">
            <el-table-column prop="name" label="站点" min-width="90" />
            <el-table-column label="热功率" align="right"><template #default="{ row }"><span class="num">{{ fmt(row.heat_mw, 2) }}</span></template></el-table-column>
            <el-table-column label="一次流量" align="right"><template #default="{ row }"><span class="num">{{ fmt(row.g1, 0) }}</span></template></el-table-column>
            <el-table-column label="室温" align="right">
              <template #default="{ row }"><span class="num" :class="row.tin < 18.5 ? 'danger' : row.tin > 22 ? 'warn' : 'ok'">{{ fmt(row.tin) }}</span></template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </div>

    <div class="grid bottom">
      <div class="panel">
        <div class="panel-title">供回水温度与流量 <span class="sub">近 48 小时</span></div>
        <EChart :option="tempOpt" height="260px" />
      </div>
      <div class="panel">
        <div class="panel-title">
          负荷预测 <span class="sub">{{ fc?.summary }}</span>
        </div>
        <EChart :option="fcOpt" height="260px" />
      </div>
    </div>
  </div>
  <div v-else-if="rt.meta" class="page">
    <el-empty description="该热源不存在，或不在你的数据范围内">
      <el-button @click="$router.back()">返回</el-button>
    </el-empty>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import KpiCard from '@/components/KpiCard.vue'
import SourceDiagram from '@/components/diagrams/SourceDiagram.vue'
import { api, getData } from '@/api/http'
import { useRealtime } from '@/stores/realtime'
import { fmt, fmtInt, KIND_TEXT } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const rt = useRealtime()
const { snap, meta } = storeToRefs(rt)
const id = computed(() => route.params.id as string)
const h = computed(() => snap.value?.sources[id.value])
const today = computed(() => (snap.value?.today.objects[id.value] ?? {}) as Record<string, number>)
const served = computed(() => Object.values(snap.value!.stations).filter((s) => s.source_id === id.value))
const hist = ref<any>(null)
const fc = ref<any>(null)

watch(
  id,
  async (v) => {
    if (!v?.startsWith('HS') || !rt.sourceMeta[v]) return   // 不在数据范围内：不发请求，显示空状态
    hist.value = null
    fc.value = null
    hist.value = await getData(`/api/history/${v}?hours=48&metrics=ts1,tr1,g,heat_mw`)
    fc.value = await api.get(`/api/ai/forecast?scope=${v}&horizon=72`)
  },
  { immediate: true },
)

const tempOpt = computed<EChartsOption>(() => {
  const d = hist.value
  if (!d) return {}
  return {
    legend: { data: ['供水温度', '回水温度', '流量'] },
    xAxis: { type: 'category', data: d.ts, axisLabel: { interval: 7 } },
    yAxis: [{ type: 'value', name: '℃', scale: true }, { type: 'value', name: 't/h', splitLine: { show: false }, scale: true }],
    series: [
      { name: '供水温度', type: 'line', data: d.ts1, showSymbol: false, smooth: true, color: '#ff6b4a' },
      { name: '回水温度', type: 'line', data: d.tr1, showSymbol: false, smooth: true, color: '#38a8ff' },
      { name: '流量', type: 'bar', yAxisIndex: 1, data: d.g, color: 'rgba(148,163,184,.35)' },
    ],
  }
})

const fcOpt = computed<EChartsOption>(() => {
  const r = fc.value?.data
  if (!r) return {}
  const ts = [...r.history.ts.slice(-24), ...r.forecast.ts]
  const pad = (a: number[], n: number) => [...Array(n).fill(null), ...a]
  const n = 24
  return {
    legend: { data: ['实际', 'P50 预测', 'P10–P90'] },
    xAxis: { type: 'category', data: ts, axisLabel: { interval: 11 } },
    yAxis: { type: 'value', name: 'MW', scale: true },
    series: [
      { name: '实际', type: 'line', data: r.history.heat_mw.slice(-24), showSymbol: false, color: '#dbe5f4' },
      { name: 'P10–P90', type: 'line', stack: 'band', data: pad(r.forecast.p10, n), lineStyle: { opacity: 0 }, showSymbol: false, color: '#3b8cff' },
      { name: 'P10–P90', type: 'line', stack: 'band', data: pad(r.forecast.p90.map((v: number, i: number) => v - r.forecast.p10[i]), n),
        lineStyle: { opacity: 0 }, areaStyle: { opacity: 0.25, color: '#3b8cff' }, showSymbol: false, color: '#3b8cff', tooltip: { show: false } },
      { name: 'P50 预测', type: 'line', data: pad(r.forecast.p50, n), showSymbol: false, color: '#3b8cff', lineStyle: { type: 'dashed', width: 2 },
        markLine: r.capacity_mw ? { symbol: 'none', data: [{ yAxis: r.capacity_mw }], label: { formatter: '装机容量' }, lineStyle: { color: '#ef4444' } } : undefined },
    ],
  }
})
</script>

<style scoped>
.head { flex-wrap: wrap; }
.top { grid-template-columns: minmax(0, 1fr) 360px; }
.side { display: flex; flex-direction: column; gap: 12px; }
.k2 { grid-template-columns: 1fr 1fr; }
.bottom { grid-template-columns: 1fr 1fr; }
.clickable :deep(tr) { cursor: pointer; }
@media (max-width: 1200px) { .top, .bottom { grid-template-columns: 1fr; } }
</style>
