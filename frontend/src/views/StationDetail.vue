<template>
  <div class="page" v-if="d && m && meta">
    <div class="row head">
      <el-select :model-value="id" @update:model-value="(v: string) => router.replace(`/stations/${v}`)" filterable size="small" style="width: 200px">
        <el-option-group v-for="s in meta.sources" :key="s.id" :label="s.name">
          <el-option v-for="sid in s.stations" :key="sid" :value="sid" :label="`${sid} ${rt.stationMeta[sid].name}`" />
        </el-option-group>
      </el-select>
      <span class="muted">{{ m.address }} · 面积 {{ fmt(m.area / 1e4, 1) }} 万m² · {{ m.system === 'radiator' ? '散热器' : '地暖' }}系统 ·
        设计热指标 {{ m.design_index }} W/m² · 热源 {{ rt.sourceMeta[m.source_id].name }}</span>
      <el-tag :type="m.autonomy === 'L3' ? 'success' : 'info'" size="small">自治等级 {{ m.autonomy }}</el-tag>
      <el-tag v-for="a in alarms" :key="a.id" :type="LEVEL_TYPE[a.level]" size="small" effect="dark">{{ a.message }}</el-tag>
    </div>

    <div class="grid top">
      <div class="panel">
        <div class="panel-title">换热站工艺图 <span class="sub">实时 · 每 2 秒刷新</span></div>
        <StationDiagram :d="d" />
      </div>
      <div class="side">
        <div class="grid k2">
          <KpiCard label="热功率" :value="fmt(d.heat_mw, 2)" unit="MW" tone="supply" :sub="`今日 ${fmt(today.heat_gj, 1)} GJ`" />
          <KpiCard label="典型室温" :value="fmt(d.tin)" unit="℃" :tone="d.tin < 18.5 ? 'danger' : d.tin > 22 ? 'warn' : 'ok'" sub="目标 18–22℃" />
          <KpiCard label="一次温差" :value="fmt(d.ts1 - d.tr1)" unit="℃" :sub="`二次温差 ${fmt(d.ts2 - d.tr2)}℃`" />
          <KpiCard label="热量平衡" :value="fmt(((d.q1_mw - d.q2_mw) / Math.max(d.q2_mw, 0.1)) * 100)" unit="%"
                   :tone="Math.abs(d.q1_mw - d.q2_mw) / Math.max(d.q2_mw, 0.1) > 0.1 ? 'warn' : ''" sub="一次侧 vs 二次侧" />
          <KpiCard label="今日电耗" :value="fmt(today.elec_kwh, 0)" unit="kWh" />
          <KpiCard label="今日补水" :value="fmt(today.water_t, 2)" unit="t" :tone="d.makeup_tph > 0.5 ? 'danger' : ''" />
        </div>
        <div class="panel" v-if="policy">
          <div class="panel-title">安全边界 <span class="sub">{{ policy.auto_sign ? '边界内自动签发' : '需人工确认' }}</span></div>
          <div class="kv"><span class="k">阀门开度</span><span>{{ policy.params.valve_opening.min }}–{{ policy.params.valve_opening.max }}%，单次 ≤ {{ policy.params.valve_opening.max_step }}%</span></div>
          <div class="kv"><span class="k">二次供温</span><span>{{ policy.params.supply_temp_setpoint.min }}–{{ policy.params.supply_temp_setpoint.max }}℃，单次 ≤ {{ policy.params.supply_temp_setpoint.max_step }}℃</span></div>
          <div class="kv"><span class="k">泵频率</span><span>{{ policy.params.pump_frequency.min }}–{{ policy.params.pump_frequency.max }}Hz，单次 ≤ {{ policy.params.pump_frequency.max_step }}Hz</span></div>
          <div class="kv"><span class="k">频次</span><span>每小时 ≤ {{ policy.max_cmds_per_hour }} 次</span></div>
          <div v-for="t in policy.interlocks" :key="t" class="lock muted">· {{ t }}</div>
        </div>
      </div>
    </div>

    <!-- 远程控制 -->
    <div class="panel" v-if="settings && policy">
      <div class="panel-title">
        远程控制
        <span class="sub">当前：{{ MODE_TEXT[settings.mode] }} · 阀门设定 {{ fmt(settings.valve_set) }}% · 泵 {{ fmt(settings.freq) }}Hz
          · 所有指令经“草拟 → 安全校验 → 审批 → 下发 → 跟踪”</span>
      </div>
      <el-tabs v-model="tab">
        <el-tab-pane label="气候补偿曲线" name="curve">
          <ClimateCurveEditor v-model="newCurve" :current="settings.curve" :recommended="aiCurve" :tout="d.tout"
                              :lo="policy.params.climate_curve.min" :hi="policy.params.climate_curve.max" />
          <div class="act">
            <el-input v-model="reason" placeholder="调整原因（写入审计）" size="small" style="max-width: 360px" />
            <el-button type="primary" :disabled="!curveChanged" @click="send('climate_curve', newCurve)">下发曲线</el-button>
            <span v-if="aiSummary" class="muted small">AI：{{ aiSummary }}</span>
          </div>
        </el-tab-pane>
        <el-tab-pane label="阀门开度" name="valve">
          <div class="ctl">
            <div class="ctl-row">
              <span class="lbl">目标开度</span>
              <el-slider v-model="valve" :min="0" :max="100" :step="0.5" show-input :marks="valveMarks" style="flex: 1" />
            </div>
            <el-alert type="info" :closable="false" show-icon
                      :title="`设定阀门开度将切换为“手动阀位”模式，气候补偿暂停。当前开度 ${fmt(d.valve)}%，本次调整 ${fmt(valve - settings.valve_set)}%（单次上限 ${policy.params.valve_opening.max_step}%）。`" />
            <div class="act">
              <el-input v-model="reason" placeholder="调整原因" size="small" style="max-width: 360px" />
              <el-button type="primary" @click="send('valve_opening', valve)">下发阀门开度</el-button>
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="控制模式 / 定供温" name="mode">
          <div class="ctl">
            <div class="ctl-row">
              <span class="lbl">控制模式</span>
              <el-radio-group v-model="mode">
                <el-radio-button value="curve">气候补偿</el-radio-button>
                <el-radio-button value="fixed">定供温</el-radio-button>
                <el-radio-button value="manual">手动阀位</el-radio-button>
              </el-radio-group>
              <el-button :disabled="mode === settings.mode" @click="send('control_mode', mode)">切换模式</el-button>
            </div>
            <div class="ctl-row">
              <span class="lbl">二次供温设定</span>
              <el-input-number v-model="ts2" :step="0.5" :precision="1" :min="policy.params.supply_temp_setpoint.min" :max="policy.params.supply_temp_setpoint.max" />
              <span class="muted">℃（当前 {{ fmt(d.ts2_set) }}℃）</span>
              <el-button type="primary" @click="send('supply_temp_setpoint', ts2)">下发定供温</el-button>
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="循环泵频率" name="pump">
          <div class="ctl">
            <div class="ctl-row">
              <span class="lbl">2# 循环泵频率</span>
              <el-slider v-model="freq" :min="25" :max="50" :step="0.5" show-input style="flex: 1" />
            </div>
            <div class="muted small">频率影响二次流量（∝f）与泵功率（∝f³）。降低频率可节电，但会增大二次温差、降低末端室温。</div>
            <div class="act"><el-button type="primary" @click="send('pump_frequency', freq)">下发频率</el-button></div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <div class="grid bottom">
      <div class="panel">
        <div class="panel-title">运行趋势 <span class="sub">近 48 小时逐时</span></div>
        <EChart :option="trendOpt" height="300px" />
      </div>
      <div class="panel">
        <div class="panel-title">本站调控记录</div>
        <el-table :data="cmds" size="small" height="300px" empty-text="暂无指令">
          <el-table-column prop="executed_at" label="时间" width="150"><template #default="{ row }">{{ row.executed_at.replace('T', ' ') }}</template></el-table-column>
          <el-table-column prop="param_text" label="参数" width="100" />
          <el-table-column prop="description" label="内容" min-width="180" show-overflow-tooltip />
          <el-table-column label="来源" width="60"><template #default="{ row }">{{ row.source === 'ai' ? 'AI' : '人工' }}</template></el-table-column>
          <el-table-column label="状态" width="76">
            <template #default="{ row }"><el-tag size="small" :type="row.status === 'executed' ? 'success' : 'info'">{{ row.status === 'executed' ? '已执行' : '已回滚' }}</el-tag></template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <CommandWizard v-model="wizard" :request="req" @done="refresh" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import KpiCard from '@/components/KpiCard.vue'
import StationDiagram from '@/components/diagrams/StationDiagram.vue'
import ClimateCurveEditor from '@/components/ClimateCurveEditor.vue'
import CommandWizard from '@/components/CommandWizard.vue'
import { api, getData } from '@/api/http'
import type { Command, CommandRequest, ControlSettings, CurvePoint, Policy } from '@/api/types'
import { useRealtime } from '@/stores/realtime'
import { fmt, LEVEL_TYPE, MODE_TEXT } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const rt = useRealtime()
const { snap, meta } = storeToRefs(rt)
const id = computed(() => route.params.id as string)
const d = computed(() => snap.value?.stations[id.value])
const m = computed(() => rt.stationMeta[id.value])
const today = computed(() => (snap.value?.today.objects[id.value] ?? {}) as Record<string, number>)
const alarms = computed(() => snap.value!.alarms.filter((a) => a.object_id === id.value))

const settings = ref<ControlSettings | null>(null)
const policy = ref<Policy | null>(null)
const cmds = ref<Command[]>([])
const hist = ref<any>(null)
const aiCurve = ref<CurvePoint[] | null>(null)
const aiSummary = ref('')
const tab = ref('curve')
const newCurve = ref<CurvePoint[]>([])
const valve = ref(50)
const mode = ref<'curve' | 'fixed' | 'manual'>('curve')
const ts2 = ref(40)
const freq = ref(45)
const reason = ref('')
const wizard = ref(false)
const req = ref<CommandRequest | null>(null)

const valveMarks = computed(() => (d.value ? { [Math.round(d.value.valve)]: '当前' } : {}))
const curveChanged = computed(() => JSON.stringify(newCurve.value) !== JSON.stringify(settings.value?.curve))

async function refresh() {
  const sid = id.value
  const [s, c] = await Promise.all([
    getData<ControlSettings>(`/api/control/settings/${sid}`),
    getData<Command[]>(`/api/control/commands?station_id=${sid}`),
  ])
  settings.value = s
  cmds.value = c
  newCurve.value = s.curve.map((p) => [p[0], p[1]])
  valve.value = s.valve_set
  mode.value = s.mode
  ts2.value = Math.round((d.value?.ts2_set ?? s.ts2_fixed) * 2) / 2
  freq.value = s.freq
}

watch(
  id,
  async (sid) => {
    if (!sid?.startsWith('ST')) return
    settings.value = null
    aiCurve.value = null
    aiSummary.value = ''
    await refresh()
    policy.value = await getData<Policy>(`/api/control/policy/${sid}`)
    hist.value = await getData(`/api/history/${sid}?hours=48&metrics=ts1,tr1,ts2,tr2,ts2_set,valve,tin,heat_mw,tout`)
    const r = await api.get<any>(`/api/ai/curve/${sid}`)
    aiCurve.value = r.data.recommended_curve
    aiSummary.value = r.summary
  },
  { immediate: true },
)

function send(param: CommandRequest['param'], value: any) {
  req.value = { station_id: id.value, param, value, reason: reason.value }
  wizard.value = true
}

const trendOpt = computed<EChartsOption>(() => {
  const h = hist.value
  if (!h) return {}
  return {
    legend: { data: ['一次供温', '一次回温', '二次供温', '二次设定', '二次回温', '阀门开度', '室温', '室外温度'], selected: { 一次供温: false, 室外温度: false } },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: h.ts, axisLabel: { interval: 7 } },
    yAxis: [{ type: 'value', name: '℃', scale: true }, { type: 'value', name: '%', min: 0, max: 100, splitLine: { show: false } }],
    series: [
      { name: '一次供温', type: 'line', data: h.ts1, showSymbol: false, color: '#ef4444' },
      { name: '一次回温', type: 'line', data: h.tr1, showSymbol: false, color: '#60a5fa' },
      { name: '二次供温', type: 'line', data: h.ts2, showSymbol: false, color: '#ff6b4a', lineStyle: { width: 2 } },
      { name: '二次设定', type: 'line', data: h.ts2_set, showSymbol: false, color: '#f5a524', lineStyle: { type: 'dashed' } },
      { name: '二次回温', type: 'line', data: h.tr2, showSymbol: false, color: '#38a8ff' },
      { name: '阀门开度', type: 'line', yAxisIndex: 1, data: h.valve, showSymbol: false, color: '#a78bfa', areaStyle: { opacity: 0.08 } },
      { name: '室温', type: 'line', data: h.tin, showSymbol: false, color: '#22c55e', lineStyle: { width: 2 } },
      { name: '室外温度', type: 'line', data: h.tout, showSymbol: false, color: '#94a3b8' },
    ],
  }
})
</script>

<style scoped>
.head { flex-wrap: wrap; gap: 10px; }
.top { grid-template-columns: minmax(0, 1fr) 380px; }
.side { display: flex; flex-direction: column; gap: 12px; }
.k2 { grid-template-columns: 1fr 1fr; }
.lock { font-size: 12px; margin-top: 4px; }
.bottom { grid-template-columns: 3fr 2fr; }
.ctl { display: flex; flex-direction: column; gap: 14px; max-width: 900px; }
.ctl-row { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.lbl { width: 96px; color: var(--muted); }
.act { display: flex; gap: 10px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
.small { font-size: 12px; }
@media (max-width: 1200px) { .top, .bottom { grid-template-columns: 1fr; } }
</style>
