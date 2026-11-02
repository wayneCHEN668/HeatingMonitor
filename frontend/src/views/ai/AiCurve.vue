<template>
  <div class="wrap">
    <div class="grid main">
      <div class="panel">
        <div class="panel-title">全网曲线优化潜力 <span class="sub">{{ overview?.summary }}</span></div>
        <el-table :data="overview?.data.rows ?? []" size="small" highlight-current-row @row-click="(r: any) => (sel = r.station_id)"
                  class="clickable" v-loading="!overview">
          <el-table-column prop="station_name" label="站点" width="100" />
          <el-table-column prop="tin_recent" label="近24h室温" align="right">
            <template #default="{ row }"><span :class="row.tin_recent > 21.5 ? 'warn' : row.tin_recent < 18.5 ? 'danger' : ''">{{ row.tin_recent }}</span></template>
          </el-table-column>
          <el-table-column prop="mean_delta" label="建议调整℃" align="right">
            <template #default="{ row }"><span :class="row.mean_delta < 0 ? 'ok' : 'warn'">{{ row.mean_delta > 0 ? '+' : '' }}{{ row.mean_delta }}</span></template>
          </el-table-column>
          <el-table-column prop="saving_gj_day" label="节热 GJ/天" align="right" />
          <el-table-column prop="r2" label="R²" align="right" width="64" />
        </el-table>
      </div>
      <div class="detail" v-loading="loading">
        <EnvelopeInfo :env="env" />
        <template v-if="d">
          <div class="grid two">
            <div class="panel">
              <div class="panel-title">{{ d.station_name }} · 曲线对比 <span class="sub">目标室温 {{ d.target }}℃</span></div>
              <EChart :option="curveOpt" height="280px" not-merge />
            </div>
            <div class="panel">
              <div class="panel-title">模型辨识 <span class="sub">(Tin−Tout) = α·(Tm−Tout)</span></div>
              <EChart :option="fitOpt" height="280px" not-merge />
            </div>
          </div>
          <div class="panel">
            <div class="grid params">
              <div class="kv"><span class="k">α（散热器/围护传热比）</span><span class="num">{{ d.model.alpha }}</span></div>
              <div class="kv"><span class="k">β（二次温差系数）</span><span class="num">{{ d.model.beta }}</span></div>
              <div class="kv"><span class="k">R² / 室温 RMSE</span><span class="num">{{ d.model.r2 }} / {{ d.model.rmse }}℃</span></div>
              <div class="kv"><span class="k">样本</span><span class="num">{{ d.model.n }} h（近 {{ d.model.days }} 天）</span></div>
              <div class="kv"><span class="k">Ts2–Tout 相关系数</span><span class="num">{{ d.model.collinearity }}</span></div>
              <div class="kv"><span class="k">预计节热</span><span class="num" :class="d.saving_pct > 0 ? 'ok' : 'warn'">{{ d.saving_pct }}% · {{ d.saving_gj_day }} GJ/天</span></div>
            </div>
            <div class="act">
              <el-button v-if="auth.has('control:draft')" type="success" @click="sendDraft">生成调控草案并走审批流程</el-button>
              <el-button @click="router.push(`/stations/${sel}`)">打开站点控制页</el-button>
              <span class="muted small">AI 只生成方案，下发须经过安全校验与令牌确认。</span>
            </div>
          </div>
        </template>
      </div>
    </div>
    <CommandWizard v-model="wizard" :request="req" @done="load" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import CommandWizard from '@/components/CommandWizard.vue'
import EnvelopeInfo from './EnvelopeInfo.vue'
import { api } from '@/api/http'
import { useAuth } from '@/stores/auth'
import type { CommandRequest, Envelope } from '@/api/types'

const props = defineProps<{ station: string }>()
const router = useRouter()
const auth = useAuth()
const overview = ref<Envelope<any> | null>(null)
const env = ref<Envelope<any> | null>(null)
const d = computed(() => env.value?.data)
const sel = ref(props.station || '')
const loading = ref(false)
const wizard = ref(false)
const req = ref<CommandRequest | null>(null)

onMounted(async () => {
  overview.value = await api.get('/api/ai/curve')
  if (!sel.value) sel.value = overview.value.data.rows[0].station_id
})
async function load() {
  if (!sel.value) return
  loading.value = true
  try {
    env.value = await api.get(`/api/ai/curve/${sel.value}`)
  } finally {
    loading.value = false
  }
}
watch(sel, load, { immediate: true })

function sendDraft() {
  req.value = { station_id: sel.value, param: 'climate_curve', value: d.value.recommended_curve, reason: env.value!.summary, source: 'ai' }
  wizard.value = true
}

const curveOpt = computed<EChartsOption>(() => {
  const x = d.value
  if (!x) return {}
  return {
    legend: {},
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'value', name: '室外温度 ℃', nameLocation: 'middle', nameGap: 24, min: -28, max: 12 },
    yAxis: { type: 'value', name: '二次供温 ℃', scale: true },
    series: [
      { name: '当前曲线', type: 'line', data: x.current_curve, color: '#94a3b8', lineStyle: { type: 'dashed' } },
      { name: 'AI 推荐', type: 'line', data: x.recommended_curve, color: '#22c55e', lineStyle: { width: 3 }, symbolSize: 8,
        markArea: { silent: true, itemStyle: { color: 'rgba(245,165,36,.07)' }, label: { show: true, color: '#8797b3', formatter: '外推区' },
                    data: [[{ xAxis: -28 }, { xAxis: x.tout_range[0] }], [{ xAxis: x.tout_range[1] }, { xAxis: 12 }]] } },
    ],
  }
})
const fitOpt = computed<EChartsOption>(() => {
  const x = d.value
  if (!x) return {}
  const pts = x.scatter.x.map((v: number, i: number) => [v, x.scatter.y[i]])
  const mx = Math.max(...x.scatter.x)
  return {
    tooltip: { trigger: 'item' },
    legend: {},
    xAxis: { type: 'value', name: 'Tm − Tout ℃', nameLocation: 'middle', nameGap: 24, scale: true },
    yAxis: { type: 'value', name: 'Tin − Tout ℃', scale: true },
    series: [
      { name: '逐时样本', type: 'scatter', data: pts, symbolSize: 4, color: '#3b8cff' },
      { name: `拟合 α=${x.model.alpha}`, type: 'line', data: [[0, 0], [mx, mx * x.model.alpha]], showSymbol: false, color: '#f5a524' },
    ],
  }
})
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; }
.main { grid-template-columns: 430px minmax(0, 1fr); align-items: start; }
.detail { display: flex; flex-direction: column; gap: 12px; }
.two { grid-template-columns: 1fr 1fr; }
.params { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 24px; }
.act { display: flex; gap: 10px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
.small { font-size: 12px; }
.clickable :deep(tr) { cursor: pointer; }
@media (max-width: 1300px) { .main, .two { grid-template-columns: 1fr; } .params { grid-template-columns: 1fr 1fr; } }
</style>
