<template>
  <div class="page" v-if="snap">
    <div class="grid cards">
      <div v-for="h in sources" :key="h.id" class="panel card" @click="router.push(`/sources/${h.id}`)">
        <div class="panel-title">
          <span>{{ h.name }} <el-tag size="small" effect="plain">{{ KIND_TEXT[h.kind] }}</el-tag></span>
          <el-button size="small" text type="primary">工艺图 →</el-button>
        </div>
        <div class="body">
          <EChart :option="gauge(h)" height="150px" />
          <div class="vals">
            <div class="kv"><span class="k">供水温度</span><span class="num supply">{{ fmt(h.ts1) }} ℃</span></div>
            <div class="kv"><span class="k">回水温度</span><span class="num return">{{ fmt(h.tr1) }} ℃</span></div>
            <div class="kv"><span class="k">供/回压力</span><span class="num">{{ fmt(h.p_s, 3) }} / {{ fmt(h.p_r, 3) }} MPa</span></div>
            <div class="kv"><span class="k">循环流量</span><span class="num">{{ fmt(h.g, 0) }} t/h</span></div>
            <div class="kv"><span class="k">{{ energyLabel(h).k }}</span><span class="num">{{ energyLabel(h).v }}</span></div>
            <div class="kv"><span class="k">今日供热</span><span class="num">{{ fmtInt(snap.today.objects[h.id]?.heat_gj) }} GJ</span></div>
          </div>
        </div>
        <div class="units">
          <el-tooltip v-for="u in h.units" :key="u.id" :content="unitTip(u)" placement="top">
            <span class="unit" :class="{ on: u.running }">
              <i class="dot" :class="u.running ? 'on' : 'off'" />{{ u.name.replace(/\(备\)/, '') }}
            </span>
          </el-tooltip>
        </div>
        <EChart :option="mini(h.id)" height="90px" />
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">多热源出力对比 <span class="sub">近 7 天逐时，堆叠</span></div>
      <EChart :option="stackOpt" height="300px" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import { getData } from '@/api/http'
import type { SourceRT, UnitRT } from '@/api/types'
import { useRealtime } from '@/stores/realtime'
import { fmt, fmtInt, KIND_TEXT } from '@/utils/format'

defineOptions({ name: 'SourceList' })
const router = useRouter()
const { snap } = storeToRefs(useRealtime())
const sources = computed(() => Object.values(snap.value!.sources))
const hist = ref<Record<string, any>>({})

onMounted(async () => {
  const ids = Object.keys(snap.value!.sources)
  const res = await Promise.all(ids.map((id) => getData<any>(`/api/history/${id}?hours=168&metrics=heat_mw,ts1,tr1,tout`)))
  hist.value = Object.fromEntries(ids.map((id, i) => [id, res[i]]))
})

function energyLabel(h: SourceRT) {
  if (h.kind === 'chp') return { k: '抽汽量', v: `${fmt(h.steam_tph)} t/h` }
  if (h.kind === 'gas') return { k: '燃气量 / 效率', v: `${fmt(h.gas_nm3h, 0)} Nm³/h · ${fmt(h.eff)}%` }
  return { k: '用电 / 蓄热', v: `${fmt((h.power_kw ?? 0) / 1000, 2)} MW · ${fmt(h.tank_soc, 0)}%` }
}
function unitTip(u: UnitRT) {
  const parts = [u.running ? '运行' : '停机']
  if (u.load !== undefined) parts.push(`负荷 ${fmt(u.load, 0)}%`)
  if (u.freq) parts.push(`${fmt(u.freq)} Hz`)
  if (u.soc !== undefined) parts.push(`蓄热 ${fmt(u.soc, 0)}% ${u.mode}`)
  return `${u.name}：${parts.join(' · ')}`
}
function gauge(h: SourceRT): EChartsOption {
  return {
    series: [{
      type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100, radius: '95%', center: ['50%', '58%'],
      progress: { show: true, width: 10, itemStyle: { color: h.load_pct > 90 ? '#ef4444' : h.load_pct > 75 ? '#f5a524' : '#3b8cff' } },
      axisLine: { lineStyle: { width: 10, color: [[1, '#1d2a44']] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, pointer: { show: false },
      title: { offsetCenter: [0, '40%'], color: '#8797b3', fontSize: 12 },
      detail: { offsetCenter: [0, '-2%'], fontSize: 22, color: '#f1f6ff', formatter: () => `${fmt(h.heat_mw)}MW` },
      data: [{ value: +h.load_pct.toFixed(1), name: `负荷率 ${fmt(h.load_pct)}%` }],
    }],
  }
}
function mini(id: string): EChartsOption {
  const h = hist.value[id]
  if (!h) return {}
  const n = 48
  return {
    grid: { left: 4, right: 4, top: 6, bottom: 4, containLabel: false },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: h.ts.slice(-n), show: false },
    yAxis: { type: 'value', show: false, scale: true },
    series: [{ name: '热负荷MW', type: 'line', data: h.heat_mw.slice(-n), showSymbol: false, smooth: true, areaStyle: { opacity: 0.2 }, color: '#ff6b4a' }],
  }
}
const stackOpt = computed<EChartsOption>(() => {
  const ids = Object.keys(hist.value)
  if (!ids.length) return {}
  const first = hist.value[ids[0]]
  return {
    legend: { data: [...ids.map((id) => snap.value!.sources[id].name), '室外温度'] },
    tooltip: { trigger: 'axis' },
    dataZoom: [{ type: 'inside' }],
    xAxis: { type: 'category', data: first.ts, axisLabel: { interval: 23 } },
    yAxis: [{ type: 'value', name: 'MW' }, { type: 'value', name: '℃', splitLine: { show: false } }],
    series: [
      ...ids.map((id, i) => ({
        name: snap.value!.sources[id].name, type: 'line' as const, stack: 'heat', areaStyle: { opacity: 0.55 }, showSymbol: false,
        data: hist.value[id].heat_mw, color: ['#ff6b4a', '#f5a524', '#a78bfa'][i],
      })),
      { name: '室外温度', type: 'line', yAxisIndex: 1, data: first.tout, showSymbol: false, color: '#38a8ff', lineStyle: { type: 'dashed' } },
    ],
  }
})
</script>

<style scoped>
.cards { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.card { cursor: pointer; transition: border-color .2s; }
.card:hover { border-color: #3b8cff; }
.body { display: grid; grid-template-columns: 150px 1fr; gap: 10px; align-items: center; }
.units { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 6px; }
.unit { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #1a2235; color: #8797b3; display: inline-flex; align-items: center; }
.unit.on { background: #0f2a1c; color: #a7f3c4; }
@media (max-width: 1300px) { .cards { grid-template-columns: 1fr; } }
</style>
