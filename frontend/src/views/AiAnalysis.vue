<template>
  <div class="page">
    <el-tabs v-model="tab">
      <el-tab-pane label="综合诊断" name="diag"><AiDiagnosis v-if="tab === 'diag'" @goto="goto" /></el-tab-pane>
      <el-tab-pane label="负荷预测" name="forecast"><AiForecast v-if="tab === 'forecast'" :scope="scope" /></el-tab-pane>
      <el-tab-pane label="异常检测" name="anomaly"><AiAnomaly v-if="tab === 'anomaly'" :station="station" /></el-tab-pane>
      <el-tab-pane label="一站一策曲线优化" name="curve"><AiCurve v-if="tab === 'curve'" :station="station" /></el-tab-pane>
      <el-tab-pane label="调控效果评估" name="effect"><AiEffect v-if="tab === 'effect'" /></el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AiDiagnosis from './ai/AiDiagnosis.vue'
import AiForecast from './ai/AiForecast.vue'
import AiAnomaly from './ai/AiAnomaly.vue'
import AiCurve from './ai/AiCurve.vue'
import AiEffect from './ai/AiEffect.vue'

const route = useRoute()
const router = useRouter()
const tab = ref((route.query.tab as string) || 'diag')
const scope = ref((route.query.scope as string) || 'NET')
const station = ref((route.query.station as string) || '')
watch(tab, (t) => router.replace({ query: { ...route.query, tab: t } }))

function goto(action: { type: string; station_id?: string; scope?: string }) {
  if (action.type === 'curve_opt') { station.value = action.station_id!; tab.value = 'curve' }
  else if (action.type === 'anomaly') { station.value = action.station_id!; tab.value = 'anomaly' }
  else if (action.type === 'forecast') { scope.value = action.scope || 'NET'; tab.value = 'forecast' }
}
</script>
