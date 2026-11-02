<template>
  <div class="wrap" v-loading="loading">
    <div class="panel bar">
      <el-date-picker v-model="range" type="daterange" value-format="YYYY-MM-DD" size="small" :clearable="false"
                      start-placeholder="开始" end-placeholder="结束" :disabled-date="future" />
      <el-radio-group v-model="quick" size="small">
        <el-radio-button :value="3">近3天</el-radio-button>
        <el-radio-button :value="7">近7天</el-radio-button>
        <el-radio-button :value="14">本采暖季</el-radio-button>
      </el-radio-group>
      <el-tooltip :content="r?.method" placement="bottom" :disabled="!r">
        <span class="muted small">口径说明 ⓘ</span>
      </el-tooltip>
    </div>

    <template v-if="r">
      <div class="grid kpis">
        <KpiCard label="度日数修正单耗" :value="fmt(n.norm_heat)" unit="kJ/(m²·℃·d)" :sub="`上季同期 ${fmt(n.base_norm_heat)}`" />
        <KpiCard label="同比节能率" :value="pct(n.saving_rate)" :tone="n.saving_rate > 0 ? 'ok' : 'danger'" sub="已扣除天气影响" />
        <KpiCard label="节约热量" :value="fmtInt(n.saved_gj)" unit="GJ" tone="ok" :sub="`期间供热 ${fmtInt(n.heat_gj)} GJ`" />
        <KpiCard label="折合标煤" :value="fmt(n.saved_tce)" unit="tce" :sub="`1 GJ ≈ 34.12 kgce`" />
        <KpiCard label="减少 CO₂" :value="fmt(n.saved_tco2)" unit="t" sub="2.66 kgCO₂/kgce" />
        <KpiCard label="过热可挖潜" :value="fmtInt(n.overheat_gj)" unit="GJ" tone="warn" sub="室温降至 20℃ 可节约" />
        <KpiCard label="度日数 HDD" :value="fmt(n.hdd)" unit="℃·d" :sub="`室外均温 ${fmt(n.tout_avg)}℃`" />
      </div>

      <div class="grid two">
        <div class="panel">
          <div class="panel-title">修正单耗逐日对比 <span class="sub">本季 vs 上季同期（同日历日）</span></div>
          <EChart :option="trendOpt" height="280px" />
        </div>
        <div class="panel">
          <div class="panel-title">室温–能耗矩阵 <span class="sub">右上象限 = 过量供热，左上 = 能效差且不热</span></div>
          <EChart :option="scatterOpt" height="280px" />
        </div>
      </div>

      <div class="grid two">
        <div class="panel">
          <div class="panel-title">热力站修正单耗对标 <span class="sub">中位数 {{ r.median_norm_heat }}</span></div>
          <EChart :option="rankOpt" height="360px" />
        </div>
        <div class="panel">
          <div class="panel-title">热源能效与成本</div>
          <el-table :data="r.sources" size="small">
            <el-table-column prop="name" label="热源" min-width="130" />
            <el-table-column prop="heat_gj" label="供热 GJ" align="right" />
            <el-table-column label="单位成本" align="right">
              <template #default="{ row }">{{ fmt(row.unit_cost) }} <small class="muted">元/GJ</small></template>
            </el-table-column>
            <el-table-column label="能效" align="right">
              <template #default="{ row }">
                <span v-if="row.eff">锅炉效率 {{ row.eff }}%</span>
                <span v-else-if="row.cop_sys">系统COP {{ row.cop_sys }}</span>
                <span v-else class="muted">抽汽 {{ fmtInt(row.steam_t) }} t</span>
              </template>
            </el-table-column>
            <el-table-column prop="load_rate_max" label="最大负荷率%" align="right" />
          </el-table>
          <EChart :option="costOpt" height="200px" />
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">热力站节能明细</div>
        <el-table :data="r.stations" size="small" :default-sort="{ prop: 'rank', order: 'ascending' }">
          <el-table-column prop="rank" label="排名" width="64" sortable />
          <el-table-column prop="name" label="站点" width="110" />
          <el-table-column prop="area_wm2" label="面积万m²" align="right" />
          <el-table-column prop="heat_gj" label="热量 GJ" sortable align="right" />
          <el-table-column prop="unit_heat" label="GJ/万m²" sortable align="right" />
          <el-table-column prop="norm_heat" label="修正单耗" sortable align="right" />
          <el-table-column prop="vs_median" label="较中位数%" sortable align="right">
            <template #default="{ row }"><span :class="row.vs_median > 5 ? 'danger' : row.vs_median < -5 ? 'ok' : ''">{{ pct(row.vs_median, 1, true) }}</span></template>
          </el-table-column>
          <el-table-column prop="base_norm_heat" label="上季同期" align="right" />
          <el-table-column prop="saving_rate" label="同比节能%" sortable align="right">
            <template #default="{ row }"><span :class="row.saving_rate > 0 ? 'ok' : 'danger'">{{ pct(row.saving_rate) }}</span></template>
          </el-table-column>
          <el-table-column prop="saved_gj" label="节约 GJ" sortable align="right" />
          <el-table-column prop="tin_avg" label="室温" sortable align="right" />
          <el-table-column prop="overheat_potential" label="过热潜力%" sortable align="right" />
          <el-table-column prop="unit_elec" label="kWh/千m²" sortable align="right" />
          <el-table-column prop="unit_water" label="kg/千m²" sortable align="right">
            <template #default="{ row }"><span :class="row.unit_water > 60 ? 'danger' : ''">{{ row.unit_water }}</span></template>
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
import KpiCard from '@/components/KpiCard.vue'
import { getData, qs } from '@/api/http'
import { fmt, fmtInt, pct, todayStr } from '@/utils/format'

const quick = ref<number | null>(7)
const range = ref<[string, string]>([todayStr(-7), todayStr(0)])
const r = ref<any>(null)
const loading = ref(false)
const n = computed(() => r.value?.network ?? {})
const future = (d: Date) => d.getTime() > Date.now()

watch(quick, (q) => {
  if (q) range.value = [todayStr(q === 14 ? -14 : -q), todayStr(0)]
})
async function load() {
  loading.value = true
  try {
    const end = range.value[1] === todayStr(0) ? undefined : `${range.value[1]}T23:59:59`
    r.value = await getData(`/api/analytics/energy${qs({ start: `${range.value[0]}T00:00:00`, end })}`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
watch(range, load, { immediate: true })

const trendOpt = computed<EChartsOption>(() => {
  const t = r.value?.trend ?? []
  return {
    legend: {},
    xAxis: { type: 'category', data: t.map((x: any) => x.date) },
    yAxis: [{ type: 'value', name: 'kJ/(m²·℃·d)', scale: true }, { type: 'value', name: '℃', splitLine: { show: false } }],
    series: [
      { name: '本季', type: 'bar', data: t.map((x: any) => x.norm_heat), color: '#3b8cff', barMaxWidth: 26 },
      { name: '上季同期', type: 'bar', data: t.map((x: any) => x.base_norm_heat), color: '#475569', barMaxWidth: 26 },
      { name: '室外温度(本季)', type: 'line', yAxisIndex: 1, data: t.map((x: any) => x.tout), color: '#38a8ff' },
      { name: '室外温度(上季)', type: 'line', yAxisIndex: 1, data: t.map((x: any) => x.base_tout), color: '#94a3b8', lineStyle: { type: 'dashed' } },
    ],
  }
})
const rankOpt = computed<EChartsOption>(() => {
  const s = [...(r.value?.stations ?? [])].reverse()
  return {
    grid: { left: 84, right: 40, top: 10, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', scale: true },
    yAxis: { type: 'category', data: s.map((x: any) => x.name) },
    series: [{
      type: 'bar', barWidth: 12, name: '修正单耗',
      data: s.map((x: any) => ({ value: x.norm_heat, itemStyle: { color: x.vs_median > 8 ? '#ef4444' : x.vs_median > 3 ? '#f5a524' : '#22c55e' } })),
      label: { show: true, position: 'right', color: '#8797b3', formatter: (p: any) => p.value },
      markLine: { symbol: 'none', data: [{ xAxis: r.value?.median_norm_heat }], label: { formatter: '中位数' }, lineStyle: { color: '#94a3b8', type: 'dashed' } },
    }],
  }
})
const scatterOpt = computed<EChartsOption>(() => {
  const s = r.value?.stations ?? []
  const med = r.value?.median_norm_heat
  return {
    grid: { left: 50, right: 30, top: 20, bottom: 40 },
    tooltip: { trigger: 'item', formatter: (p: any) => `${p.data[3]}<br/>室温 ${p.data[0]}℃<br/>修正单耗 ${p.data[1]}<br/>面积 ${p.data[2]} 万m²` },
    xAxis: { type: 'value', name: '平均室温 ℃', nameLocation: 'middle', nameGap: 26, scale: true },
    yAxis: { type: 'value', name: '修正单耗', scale: true },
    series: [{
      type: 'scatter',
      data: s.map((x: any) => [x.tin_avg, x.norm_heat, x.area_wm2, x.name]),
      symbolSize: (d: any) => 8 + d[2] * 1.2,
      itemStyle: { color: (p: any) => (p.data[0] > 21.5 ? '#f5a524' : p.data[0] < 18.5 ? '#ef4444' : '#3b8cff'), opacity: 0.85 },
      label: { show: true, position: 'top', formatter: (p: any) => p.data[3], color: '#b8c6dc', fontSize: 10 },
      markLine: { symbol: 'none', silent: true, lineStyle: { color: '#475569', type: 'dashed' },
                  data: [{ xAxis: 20 }, ...(med ? [{ yAxis: med }] : [])], label: { show: false } },
    }],
  }
})
const costOpt = computed<EChartsOption>(() => {
  const s = r.value?.sources ?? []
  return {
    grid: { left: 50, right: 20, top: 30, bottom: 24 },
    legend: {},
    xAxis: { type: 'category', data: s.map((x: any) => x.name.slice(0, 4)) },
    yAxis: { type: 'value', name: '元/GJ' },
    series: [
      { name: '本期单位成本', type: 'bar', data: s.map((x: any) => x.unit_cost), color: '#f5a524', barMaxWidth: 30 },
      { name: '上季同期', type: 'bar', data: s.map((x: any) => x.base_unit_cost), color: '#475569', barMaxWidth: 30 },
    ],
  }
})
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; }
.small { font-size: 12px; cursor: help; }
.kpis { grid-template-columns: repeat(7, minmax(0, 1fr)); }
.two { grid-template-columns: 1fr 1fr; }
@media (max-width: 1300px) { .kpis { grid-template-columns: repeat(4, 1fr); } .two { grid-template-columns: 1fr; } }
</style>
