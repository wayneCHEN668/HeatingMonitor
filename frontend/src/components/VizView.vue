<template>
  <div class="viz">
    <!-- 图表：选项由这里按受限规格拼出，标题、序列名都是纯文本 -->
    <EChart v-if="v.mode === 'chart'" :option="option" :height="height" not-merge />
    <!-- 管网定位 -->
    <template v-else-if="v.mode === 'map'">
      <NetworkTopology v-if="rt.meta && rt.snap" :meta="rt.meta" :snap="rt.snap" :highlight="v.object_ids"
                       :color-by="v.color_by" :level="v.level" @select="go" />
      <div v-else class="muted">实时数据加载中…</div>
    </template>
    <!-- 工艺图定位 -->
    <template v-else-if="v.mode === 'twin'">
      <StationDiagram v-if="station" :d="station" :highlight="v.parts" :hl-level="v.level" />
      <SourceDiagram v-else-if="source" :d="source" :highlight="v.parts" :hl-level="v.level" />
      <div v-else class="muted">实时数据加载中…</div>
    </template>
    <el-alert v-if="v.note" :type="v.level === 'danger' ? 'error' : v.level === 'warn' ? 'warning' : 'info'" :closable="false"
              show-icon :title="v.note" class="note" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import NetworkTopology from '@/components/diagrams/NetworkTopology.vue'
import SourceDiagram from '@/components/diagrams/SourceDiagram.vue'
import StationDiagram from '@/components/diagrams/StationDiagram.vue'
import { useRealtime } from '@/stores/realtime'

const props = withDefaults(defineProps<{ v: any; height?: string }>(), { height: '320px' })
const rt = useRealtime()
const router = useRouter()
const station = computed(() => (props.v.mode === 'twin' ? (rt.snap?.stations as any)?.[props.v.object_id] : null))
const source = computed(() => (props.v.mode === 'twin' ? (rt.snap?.sources as any)?.[props.v.object_id] : null))
const go = (id: string) => router.push(id.startsWith('ST') ? `/stations/${id}` : `/sources/${id}`)

const option = computed<EChartsOption>(() => {
  const v = props.v
  if (v.mode !== 'chart') return {}
  const right = v.series_data.some((s: any) => s.axis === 'right')
  const unit = (axis: string) => [...new Set(v.series_data.filter((s: any) => s.axis === axis).map((s: any) => s.unit))].join(' / ')
  const kind = v.type === 'area' ? 'line' : v.type
  // 参考线在自动缩放范围之外时会被裁掉：把坐标轴范围扩展到包含参考线
  const fitMarks = (axis: string) => {
    const ms = v.mark_lines.filter((m: any) => m.axis === axis).map((m: any) => m.value)
    if (!ms.length) return {}
    const lo = Math.min(...ms), hi = Math.max(...ms)
    const pad = (r: { min: number; max: number }) => (Math.max(r.max, hi) - Math.min(r.min, lo)) * 0.05 || 1
    return { min: (r: any) => +(Math.min(r.min, lo) - pad(r)).toFixed(2), max: (r: any) => +(Math.max(r.max, hi) + pad(r)).toFixed(2) }
  }
  return {
    legend: { top: 0 },
    grid: { top: 36 },
    tooltip: { trigger: v.x_kind === 'category' && v.type === 'scatter' ? 'item' : 'axis' },
    xAxis: { type: 'category', data: v.x, axisLabel: { rotate: v.x_kind === 'category' && v.x.length > 6 ? 30 : 0 } },
    yAxis: [
      { type: 'value', name: v.y_label || unit('left'), scale: v.type !== 'bar', ...fitMarks('left') },
      ...(right ? [{ type: 'value' as const, name: v.y_right_label || unit('right'), scale: true, ...fitMarks('right') }] : []),
    ],
    series: v.series_data.map((s: any, i: number) => ({
      name: s.name, type: kind, data: s.data, yAxisIndex: s.axis === 'right' ? 1 : 0, showSymbol: v.type === 'scatter',
      areaStyle: v.type === 'area' ? { opacity: 0.25 } : undefined, stack: v.stack ? s.axis : undefined, connectNulls: false,
      // 参考线挂在其坐标轴上的第一条序列
      markLine: v.series_data.findIndex((x: any) => x.axis === s.axis) === i && v.mark_lines.some((m: any) => m.axis === s.axis) ? {
        symbol: 'none', lineStyle: { type: 'dashed' },
        data: v.mark_lines.filter((m: any) => m.axis === s.axis).map((m: any) => ({ yAxis: m.value, name: m.label, label: { formatter: m.label || String(m.value), position: 'insideEndTop' } })),
      } : undefined,
    })),
  } as EChartsOption
})
</script>

<style scoped>
.note { margin-top: 8px; }
</style>
