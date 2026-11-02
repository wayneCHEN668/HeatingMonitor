<template>
  <div class="cce">
    <EChart :option="option" height="300px" @ready="onReady" />
    <div class="hint muted">拖动橙色圆点或在下表中修改各点二次供水温度；横坐标为室外温度。</div>
    <div class="pts">
      <div v-for="(p, i) in model" :key="p[0]" class="pt">
        <div class="tx muted">{{ p[0] }}℃</div>
        <el-input-number v-model="model[i][1]" :step="0.5" :precision="1" :min="lo" :max="hi" size="small"
                         controls-position="right" @change="emitUpdate" />
        <div class="delta num" :class="deltaCls(i)">{{ deltaText(i) }}</div>
      </div>
    </div>
    <div class="ops">
      <el-button size="small" @click="shift(-1)">整体 -1℃</el-button>
      <el-button size="small" @click="shift(1)">整体 +1℃</el-button>
      <el-button size="small" @click="reset">恢复当前曲线</el-button>
      <el-button v-if="recommended" size="small" type="success" plain @click="useRecommended">载入 AI 推荐曲线</el-button>
      <span class="muted mono" v-if="tout !== undefined">当前室外 {{ tout.toFixed(1) }}℃ → 新曲线设定 {{ interp(tout).toFixed(1) }}℃</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ECharts, EChartsOption } from 'echarts'
import type { CurvePoint } from '@/api/types'
import EChart from './EChart.vue'

const props = defineProps<{
  modelValue: CurvePoint[]
  current: CurvePoint[]
  recommended?: CurvePoint[] | null
  tout?: number
  lo: number
  hi: number
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: CurvePoint[]): void }>()

const model = ref<CurvePoint[]>(props.modelValue.map((p) => [p[0], p[1]]))
watch(
  () => props.modelValue,
  (v) => {
    if (JSON.stringify(v) !== JSON.stringify(model.value)) {
      model.value = v.map((p) => [p[0], p[1]])
      placeHandles()
    }
  },
)

let chart: ECharts | undefined

function interp(t: number) {
  const pts = model.value
  if (t <= pts[0][0]) return pts[0][1]
  if (t >= pts[pts.length - 1][0]) return pts[pts.length - 1][1]
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i][0]) {
      const [x0, y0] = pts[i - 1]
      const [x1, y1] = pts[i]
      return y0 + ((y1 - y0) * (t - x0)) / (x1 - x0)
    }
  }
  return pts[pts.length - 1][1]
}

const option = computed<EChartsOption>(() => ({
  animation: false,
  grid: { left: 50, right: 20, top: 34, bottom: 36 },
  tooltip: { trigger: 'axis', valueFormatter: (v: any) => `${Number(v).toFixed(1)}℃` },
  legend: { data: ['新曲线', '当前曲线', ...(props.recommended ? ['AI 推荐'] : [])] },
  xAxis: { type: 'value', min: -28, max: 12, name: '室外温度 ℃', nameLocation: 'middle', nameGap: 24 },
  yAxis: { type: 'value', min: Math.floor(props.lo / 5) * 5, max: Math.ceil(props.hi / 5) * 5, name: '二次供温 ℃' },
  series: [
    {
      name: '当前曲线', type: 'line', data: props.current, symbol: 'circle', symbolSize: 5,
      lineStyle: { type: 'dashed', color: '#64748b' }, itemStyle: { color: '#64748b' },
    },
    ...(props.recommended
      ? [{ name: 'AI 推荐', type: 'line' as const, data: props.recommended, symbol: 'diamond', symbolSize: 7,
           lineStyle: { type: 'dotted' as const, color: '#22c55e', width: 2 }, itemStyle: { color: '#22c55e' } }]
      : []),
    {
      name: '新曲线', type: 'line', data: model.value.map((p) => [...p]), symbolSize: 14,
      lineStyle: { color: '#f5a524', width: 3 }, itemStyle: { color: '#f5a524', borderColor: '#fff', borderWidth: 1 },
      markLine: props.tout !== undefined
        ? { symbol: 'none', silent: true, lineStyle: { color: '#38a8ff' }, label: { formatter: `室外 ${props.tout.toFixed(1)}℃`, color: '#38a8ff' },
            data: [{ xAxis: Number(props.tout.toFixed(1)) }] }
        : undefined,
      markArea: { silent: true, itemStyle: { color: 'rgba(239,68,68,.06)' }, data: [[{ yAxis: props.hi }, { yAxis: 200 }]] },
    },
  ],
}))

function placeHandles() {
  if (!chart) return
  chart.setOption({
    graphic: model.value.map((p, i) => ({
      type: 'circle',
      position: chart!.convertToPixel('grid', p),
      shape: { cx: 0, cy: 0, r: 10 },
      invisible: true,
      draggable: true,
      cursor: 'ns-resize',
      z: 100,
      ondrag(this: any) {
        const v = chart!.convertFromPixel('grid', [this.x, this.y]) as number[]
        const y = Math.round(Math.max(props.lo, Math.min(props.hi, v[1])) * 2) / 2
        model.value[i][1] = y
        chart!.setOption({ series: [{ name: '新曲线', data: model.value.map((q) => [...q]) }] })
      },
      ondragend() {
        emitUpdate()
        placeHandles()
      },
    })),
  })
}

function onReady(c: ECharts) {
  chart = c
  setTimeout(placeHandles, 0)
  window.addEventListener('resize', onResize)
}
const onResize = () => setTimeout(placeHandles, 50)
onBeforeUnmount(() => window.removeEventListener('resize', onResize))

watch(option, () => setTimeout(placeHandles, 0))

function emitUpdate() {
  emit('update:modelValue', model.value.map((p) => [p[0], p[1]]))
}
function shift(d: number) {
  model.value = model.value.map((p) => [p[0], Math.max(props.lo, Math.min(props.hi, Math.round((p[1] + d) * 10) / 10))])
  emitUpdate()
}
function reset() {
  model.value = props.current.map((p) => [p[0], p[1]])
  emitUpdate()
}
function useRecommended() {
  if (props.recommended) {
    model.value = props.recommended.map((p) => [p[0], p[1]])
    emitUpdate()
  }
}
const delta = (i: number) => model.value[i][1] - (props.current[i]?.[1] ?? model.value[i][1])
const deltaText = (i: number) => {
  const d = delta(i)
  return Math.abs(d) < 0.05 ? '—' : `${d > 0 ? '+' : ''}${d.toFixed(1)}`
}
const deltaCls = (i: number) => {
  const d = Math.abs(delta(i))
  return d > 8 ? 'danger' : d > 4 ? 'warn' : d > 0.05 ? 'ok' : 'muted'
}
</script>

<style scoped>
.hint { font-size: 12px; margin: 2px 0 8px; }
.pts { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 6px; }
.pt { text-align: center; }
.pt :deep(.el-input-number) { width: 100%; }
.tx { font-size: 12px; margin-bottom: 2px; }
.delta { font-size: 12px; margin-top: 2px; }
.ops { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
@media (max-width: 900px) { .pts { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
</style>
