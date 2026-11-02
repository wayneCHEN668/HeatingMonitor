<template>
  <div ref="el" class="echart" :style="{ height }" />
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'

const props = withDefaults(defineProps<{ option: EChartsOption; height?: string; notMerge?: boolean }>(), {
  height: '280px',
  notMerge: false,
})
const emit = defineEmits<{ (e: 'ready', chart: ECharts): void; (e: 'click', p: any): void }>()
const el = ref<HTMLDivElement>()
const chart = shallowRef<ECharts>()
let ro: ResizeObserver | undefined

/** 统一深色主题基础配置 */
const base: EChartsOption = {
  backgroundColor: 'transparent',
  textStyle: { color: '#b8c6dc', fontFamily: 'inherit' },
  color: ['#3b8cff', '#ff6b4a', '#22c55e', '#f5a524', '#a78bfa', '#2dd4bf', '#f472b6', '#94a3b8'],
  grid: { left: 48, right: 24, top: 36, bottom: 32, containLabel: true },
  tooltip: { trigger: 'axis', backgroundColor: '#14223b', borderColor: '#2a3a58', textStyle: { color: '#dbe5f4' } },
  legend: { textStyle: { color: '#b8c6dc' }, top: 0, type: 'scroll' },
}

function merge(opt: EChartsOption): EChartsOption {
  const o: any = { ...base, ...opt }
  for (const k of ['legend', 'tooltip', 'grid', 'textStyle'] as const) {
    const b = (base as any)[k]
    const v = (opt as any)[k]
    if (b && v && typeof v === 'object' && !Array.isArray(v)) o[k] = { ...b, ...v }
  }
  const axisStyle = (ax: any) =>
    ax && {
      axisLine: { lineStyle: { color: '#2a3a58' } },
      splitLine: { lineStyle: { color: '#1b2840' } },
      nameTextStyle: { color: '#8797b3' },
      ...ax,
      axisLabel: { color: '#8797b3', ...(ax.axisLabel ?? {}) },
    }
  for (const k of ['xAxis', 'yAxis']) {
    if (Array.isArray(o[k])) o[k] = o[k].map(axisStyle)
    else if (o[k]) o[k] = axisStyle(o[k])
  }
  return o
}

onMounted(() => {
  chart.value = echarts.init(el.value!, undefined, { renderer: 'canvas' })
  chart.value.setOption(merge(props.option))
  chart.value.on('click', (p) => emit('click', p))
  emit('ready', chart.value)
  ro = new ResizeObserver(() => chart.value?.resize())
  ro.observe(el.value!)
})
watch(
  () => props.option,
  (o) => chart.value?.setOption(merge(o), { notMerge: props.notMerge }),
  { deep: false },
)
onBeforeUnmount(() => {
  ro?.disconnect()
  chart.value?.dispose()
})
defineExpose({ chart })
</script>

<style scoped>
.echart { width: 100%; }
</style>
