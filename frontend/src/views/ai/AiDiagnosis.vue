<template>
  <div class="wrap" v-loading="!env">
    <EnvelopeInfo :env="env" />
    <div v-if="d" class="grid top">
      <div class="panel">
        <div class="panel-title">诊断结论 <span class="sub">{{ d.findings.length }} 项</span></div>
        <div v-for="(f, i) in d.findings" :key="i" class="finding" :class="f.level">
          <div class="f-head">
            <el-tag size="small" :type="f.level === 'danger' ? 'danger' : 'warning'" effect="dark">{{ f.category }}</el-tag>
            <b>{{ f.object }}</b> · {{ f.title }}
          </div>
          <div class="f-body muted">{{ f.detail }}</div>
          <div class="f-advice">建议：{{ f.advice }}
            <el-button size="small" link type="primary" @click="$emit('goto', f.action)">{{ actionText[f.action.type] }} →</el-button>
          </div>
        </div>
      </div>
      <div class="side">
        <div class="panel score">
          <div class="panel-title">全网运行评分</div>
          <EChart :option="gaugeOpt" height="170px" />
          <div class="muted small">评分 = 舒适 35% + 能效 30% + 稳定 15% + 设备健康 20%</div>
        </div>
        <div class="panel">
          <div class="panel-title">站点雷达 <el-select v-model="sel" size="small" style="width: 130px">
            <el-option v-for="s in d.scores" :key="s.station_id" :value="s.station_id" :label="s.station_name" />
          </el-select></div>
          <EChart :option="radarOpt" height="230px" />
        </div>
      </div>
    </div>
    <div v-if="d" class="panel">
      <div class="panel-title">站点运行评分（由低到高） <span class="sub">修正单耗中位数 {{ d.median_norm_heat }}</span></div>
      <el-table :data="d.scores" size="small" @row-click="(r: any) => (sel = r.station_id)" class="clickable">
        <el-table-column prop="station_name" label="站点" width="120" />
        <el-table-column label="综合" width="160">
          <template #default="{ row }"><el-progress :percentage="row.total" :color="scoreColor(row.total)" :format="(p: number) => `${p}`" /></template>
        </el-table-column>
        <el-table-column prop="comfort" label="舒适" align="right" />
        <el-table-column prop="efficiency" label="能效" align="right" />
        <el-table-column prop="stability" label="稳定" align="right" />
        <el-table-column prop="health" label="健康" align="right" />
        <el-table-column prop="tin" label="室温℃" align="right" />
        <el-table-column prop="norm_heat" label="修正单耗" align="right" />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import EnvelopeInfo from './EnvelopeInfo.vue'
import { api } from '@/api/http'
import type { Envelope } from '@/api/types'

defineEmits<{ (e: 'goto', a: any): void }>()
const env = ref<Envelope<any> | null>(null)
const d = computed(() => env.value?.data)
const sel = ref('')
const actionText: Record<string, string> = { curve_opt: '曲线优化', anomaly: '查看异常', forecast: '查看预测' }
const scoreColor = (v: number) => (v < 70 ? '#ef4444' : v < 85 ? '#f5a524' : '#22c55e')

onMounted(async () => {
  env.value = await api.get('/api/ai/diagnosis')
  sel.value = env.value.data.scores[0]?.station_id
})

const gaugeOpt = computed<EChartsOption>(() => ({
  series: [{
    type: 'gauge', min: 0, max: 100, radius: '100%', center: ['50%', '62%'], startAngle: 200, endAngle: -20,
    axisLine: { lineStyle: { width: 14, color: [[0.7, '#ef4444'], [0.85, '#f5a524'], [1, '#22c55e']] } },
    pointer: { width: 4, itemStyle: { color: '#dbe5f4' } }, axisTick: { show: false }, splitLine: { show: false },
    axisLabel: { show: false },
    detail: { fontSize: 28, color: '#fff', offsetCenter: [0, '30%'] },
    data: [{ value: d.value?.score_avg ?? 0 }],
  }],
}))
const radarOpt = computed<EChartsOption>(() => {
  const s = d.value?.scores.find((x: any) => x.station_id === sel.value)
  if (!s) return {}
  const avg = (k: string) => d.value.scores.reduce((a: number, x: any) => a + x[k], 0) / d.value.scores.length
  const keys = ['comfort', 'efficiency', 'stability', 'health']
  return {
    legend: { bottom: 0, top: 'auto' },
    radar: { indicator: [{ name: '舒适', max: 100 }, { name: '能效', max: 100 }, { name: '稳定', max: 100 }, { name: '健康', max: 100 }],
             radius: '62%', axisName: { color: '#b8c6dc' }, splitLine: { lineStyle: { color: '#22314d' } }, splitArea: { show: false } },
    series: [{ type: 'radar', data: [
      { name: s.station_name, value: keys.map((k) => s[k]), areaStyle: { opacity: 0.3 }, itemStyle: { color: '#3b8cff' } },
      { name: '全网平均', value: keys.map(avg), lineStyle: { type: 'dashed' }, itemStyle: { color: '#94a3b8' } },
    ] }],
  }
})
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 12px; min-height: 300px; }
.top { grid-template-columns: minmax(0, 1fr) 360px; }
.side { display: flex; flex-direction: column; gap: 12px; }
.finding { border-left: 3px solid var(--warn); background: #0c1528; border-radius: 4px; padding: 8px 12px; margin-bottom: 8px; }
.finding.danger { border-left-color: var(--danger); }
.f-head { display: flex; gap: 8px; align-items: center; }
.f-body { font-size: 13px; margin: 4px 0; }
.f-advice { font-size: 13px; color: #c7d6ee; }
.small { font-size: 12px; text-align: center; }
.clickable :deep(tr) { cursor: pointer; }
@media (max-width: 1200px) { .top { grid-template-columns: 1fr; } }
</style>
