<template>
  <div class="page" v-if="snap && meta">
    <div class="grid kpis">
      <KpiCard label="全网热负荷" :value="fmt(snap.network.heat_mw)" unit="MW" tone="supply"
               :sub="`装机 ${snap.network.capacity_mw}MW · 负荷率 ${fmt((snap.network.heat_mw / snap.network.capacity_mw) * 100)}%`" />
      <KpiCard label="今日供热量" :value="fmtInt(today.heat_gj)" unit="GJ" :sub="`面积热耗 ${fmt((today.heat_gj / snap.network.area) * 1e4, 2)} GJ/万m²`" />
      <KpiCard label="今日电耗" :value="fmtInt(today.elec_kwh)" unit="kWh" :sub="`当前电价 ${snap.network.elec_price} 元/kWh`" />
      <KpiCard label="今日补水" :value="fmt(today.water_t)" unit="t" :tone="today.water_t > 200 ? 'warn' : ''" sub="热源 + 热力站" />
      <KpiCard label="今日运行成本" :value="fmt(today.cost_yuan / 1e4, 2)" unit="万元" :sub="`单位成本 ${fmt(today.cost_yuan / Math.max(today.heat_gj, 1))} 元/GJ`" />
      <KpiCard label="平均室温" :value="fmt(snap.network.tin_avg)" unit="℃" :tone="snap.network.tin_avg < 18.5 ? 'danger' : 'ok'"
               :sub="`低于18℃站点 ${coldCount} 个`" />
      <KpiCard label="供热面积" :value="fmt(snap.network.area / 1e4, 0)" unit="万m²" :sub="`${meta.sources.length} 个热源 · ${meta.stations.length} 个热力站`" />
    </div>

    <div class="grid main">
      <div class="panel">
        <div class="panel-title">热网拓扑 <span class="sub">点击热源 / 热力站查看工艺图</span></div>
        <NetworkTopology :meta="meta" :snap="snap" @select="go" />
      </div>
      <div class="side">
        <div class="panel">
          <div class="panel-title">热源负荷</div>
          <div v-for="h in sources" :key="h.id" class="src-row" @click="go(h.id)">
            <div class="row between">
              <span>{{ h.name }} <el-tag size="small" effect="plain">{{ KIND_TEXT[h.kind] }}</el-tag></span>
              <span class="num">{{ fmt(h.heat_mw) }} / {{ h.capacity_mw }} MW</span>
            </div>
            <el-progress :percentage="Math.min(100, +h.load_pct.toFixed(1))" :stroke-width="10"
                         :color="h.load_pct > 90 ? '#ef4444' : h.load_pct > 75 ? '#f5a524' : '#3b8cff'" />
            <div class="muted small num">供/回 <span class="supply">{{ fmt(h.ts1) }}</span>/<span class="return">{{ fmt(h.tr1) }}</span>℃ · 流量 {{ fmt(h.g, 0) }} t/h</div>
          </div>
        </div>
        <div class="panel alarms">
          <div class="panel-title">实时报警 <router-link to="/alarms" class="sub">全部 →</router-link></div>
          <el-empty v-if="!snap.alarms.length" description="无报警" :image-size="60" />
          <div v-for="a in snap.alarms.slice(0, 8)" :key="a.id" class="alarm" @click="go(a.object_id)">
            <el-tag size="small" :type="LEVEL_TYPE[a.level]" effect="dark">{{ LEVEL_TEXT[a.level] }}</el-tag>
            <span class="obj">{{ a.object_name }}</span>
            <span class="msg">{{ a.message }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="grid bottom">
      <div class="panel">
        <div class="panel-title">全网热负荷与室外温度 <span class="sub">近 72 小时</span></div>
        <EChart :option="trendOpt" height="260px" />
      </div>
      <div class="panel">
        <div class="panel-title">热力站室温分布 <span class="sub">实时，目标 20℃</span></div>
        <EChart :option="tinOpt" height="260px" @click="(p: any) => go(p.data.id)" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { EChartsOption } from 'echarts'
import { storeToRefs } from 'pinia'
import EChart from '@/components/EChart.vue'
import KpiCard from '@/components/KpiCard.vue'
import NetworkTopology from '@/components/diagrams/NetworkTopology.vue'
import { getData } from '@/api/http'
import { useRealtime } from '@/stores/realtime'
import { fmt, fmtInt, KIND_TEXT, LEVEL_TEXT, LEVEL_TYPE } from '@/utils/format'

defineOptions({ name: 'Overview' })
const router = useRouter()
const { snap, meta } = storeToRefs(useRealtime())
const today = computed(() => snap.value!.today.network as Record<string, number>)
const sources = computed(() => Object.values(snap.value!.sources))
const coldCount = computed(() => Object.values(snap.value!.stations).filter((s) => s.tin < 18).length)
const go = (id: string) => router.push(id.startsWith('HS') ? `/sources/${id}` : `/stations/${id}`)

const hist = ref<any>(null)
async function loadHist() {
  hist.value = await getData('/api/history/NET?hours=72&metrics=heat_mw,tout,tin')
}
let t: number
onMounted(() => {
  loadHist()
  t = window.setInterval(loadHist, 5 * 60 * 1000)
})
onUnmounted(() => clearInterval(t))

const trendOpt = computed<EChartsOption>(() => {
  const h = hist.value
  if (!h) return {}
  return {
    legend: { data: ['热负荷', '室外温度'] },
    xAxis: { type: 'category', data: h.ts, axisLabel: { interval: 11 } },
    yAxis: [
      { type: 'value', name: 'MW' },
      { type: 'value', name: '℃', splitLine: { show: false } },
    ],
    series: [
      { name: '热负荷', type: 'line', data: h.heat_mw, smooth: true, showSymbol: false, areaStyle: { opacity: 0.15 }, color: '#ff6b4a' },
      { name: '室外温度', type: 'line', yAxisIndex: 1, data: h.tout, smooth: true, showSymbol: false, color: '#38a8ff' },
    ],
  }
})

const tinOpt = computed<EChartsOption>(() => {
  const st = Object.values(snap.value!.stations).sort((a, b) => a.tin - b.tin)
  return {
    grid: { left: 80, right: 30, top: 10, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', min: 15, max: 25, name: '℃' },
    yAxis: { type: 'category', data: st.map((s) => s.name) },
    series: [{
      type: 'bar', barWidth: 10,
      data: st.map((s) => ({
        value: +s.tin.toFixed(2), id: s.id,
        itemStyle: { color: s.tin < 18 ? '#ef4444' : s.tin < 18.5 ? '#f5a524' : s.tin > 22 ? '#f59e0b' : '#22c55e' },
      })),
      markLine: { symbol: 'none', data: [{ xAxis: 20 }], lineStyle: { color: '#94a3b8', type: 'dashed' }, label: { formatter: '目标' } },
      markArea: { silent: true, itemStyle: { color: 'rgba(34,197,94,.06)' }, data: [[{ xAxis: 18 }, { xAxis: 22 }]] },
    }],
  }
})
</script>

<style scoped>
.kpis { grid-template-columns: repeat(7, minmax(0, 1fr)); }
.main { grid-template-columns: minmax(0, 1fr) 380px; }
.side { display: flex; flex-direction: column; gap: 12px; }
.bottom { grid-template-columns: 3fr 2fr; }
.between { justify-content: space-between; }
.src-row { padding: 6px 0 10px; cursor: pointer; border-bottom: 1px dashed #1d2a44; }
.src-row:last-child { border-bottom: 0; }
.small { font-size: 12px; margin-top: 4px; }
.alarms { flex: 1; overflow: hidden; }
.alarm { display: flex; gap: 8px; align-items: center; padding: 6px 0; font-size: 13px; cursor: pointer; border-bottom: 1px dashed #1d2a44; }
.alarm .obj { color: #e8f0fc; white-space: nowrap; }
.alarm .msg { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 1400px) { .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 1100px) { .main, .bottom { grid-template-columns: 1fr; } .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
