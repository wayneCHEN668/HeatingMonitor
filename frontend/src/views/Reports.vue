<template>
  <div class="page">
    <!-- ============ 查看报告 ============ -->
    <template v-if="route.params.id">
      <div class="row top">
        <el-button size="small" @click="router.push('/reports')">← 返回列表</el-button>
        <el-button size="small" type="primary" :disabled="!rep" @click="openHtml">打开打印版（可另存为 PDF）</el-button>
      </div>
      <div class="panel doc" v-loading="!rep">
        <template v-if="rep">
          <h1>{{ rep.title }}</h1>
          <div class="muted small">{{ rep.type_name }} · 范围 {{ rep.scope_name }} · 期间 {{ rep.period.label }} · 生成于
            {{ rep.generated_at.replace('T', ' ') }} · {{ rep.generated_by }}
            <el-tag size="small" :type="rep.status === '已审核' || rep.status === '已生成' ? 'success' : rep.status === '已作废' ? 'info' : 'warning'">{{ rep.status }}</el-tag>
          </div>
          <el-alert :type="rep.check.ok ? 'success' : 'error'" :closable="false" show-icon class="mt"
                    :title="`数字核对${rep.check.ok ? '通过' : '未通过'}：正文 ${rep.check.numbers_checked} 个数字均可追溯到 ${rep.check.facts_registered} 项登记数据`"
                    :description="`数据指纹 ${rep.fingerprint} · ${rep.check.rule}`" />
          <el-alert v-for="w in rep.warnings" :key="w" type="warning" :closable="false" show-icon :title="w" class="mt" />
          <h2>关键结论</h2>
          <ul class="find"><li v-for="f in rep.key_findings" :key="f">{{ f }}</li></ul>
          <template v-for="s in rep.sections" :key="s.heading">
            <h2>{{ s.heading }} <el-tag v-if="s.human_written" size="small" type="info">人工填写</el-tag></h2>
            <p v-for="(p, i) in s.paragraphs" :key="i">{{ p }}</p>
            <EChart v-if="s.chart" :option="chartOption(s.chart)" height="260px" />
            <el-table v-if="s.table?.rows?.length" :data="s.table.rows" size="small" max-height="420" class="mt">
              <el-table-column v-for="[k, label] in s.table.columns" :key="k" :prop="k" :label="label"
                               :min-width="k === 'text' || k === 'name' || k === 'metric' || k === 'item' ? 160 : 90" show-overflow-tooltip>
                <template #default="{ row }">{{ cell(row[k]) }}</template>
              </el-table-column>
            </el-table>
          </template>
          <h2>口径说明</h2>
          <ul class="method"><li v-for="m in rep.method" :key="m">{{ m }}</li></ul>
          <details class="facts">
            <summary>登记数据（{{ Object.keys(rep.facts).length }} 项）</summary>
            <el-table :data="factRows" size="small" max-height="360">
              <el-table-column prop="key" label="键" width="180" />
              <el-table-column prop="text" label="正文中的值" width="120" />
              <el-table-column prop="value" label="原始值" width="140" />
              <el-table-column prop="unit" label="单位" width="120" />
              <el-table-column prop="source" label="来源" />
            </el-table>
          </details>
          <div v-if="rep.type === 'incident' && rep.status === '草稿（待审核）' && auth.has('report:review')" class="review">
            <h2>审核</h2>
            <el-input v-model="conclusion" type="textarea" :rows="3" placeholder="原因、责任与整改措施（审核通过时必填，作为人工结论附在报告末尾）" />
            <div class="row mt">
              <el-button type="primary" @click="review(true)">审核通过</el-button>
              <el-button @click="review(false)">作废</el-button>
            </div>
          </div>
        </template>
      </div>
    </template>

    <!-- ============ 生成与列表 ============ -->
    <template v-else>
      <div v-if="auth.has('report:write')" class="panel">
        <div class="panel-title"><span>生成报告 <span class="sub">数字由程序按固定口径计算；正文中每个数字都与登记数据核对，对不上则不生成</span></span></div>
        <el-form inline size="small">
          <el-form-item label="类型">
            <el-radio-group v-model="f.report_type">
              <el-radio-button value="daily">运行日报</el-radio-button>
              <el-radio-button value="weekly">运行周报</el-radio-button>
              <el-radio-button value="energy">节能分析</el-radio-button>
              <el-radio-button v-if="meta?.can_incident" value="incident">事故报告</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="f.report_type !== 'incident'" label="范围">
            <el-select v-model="f.scope_id" filterable style="width: 180px">
              <el-option value="NET" :label="auth.me?.scope.global ? '全网' : '本片区'" />
              <el-option-group label="热源"><el-option v-for="s in sources" :key="s.id" :value="s.id" :label="s.name" /></el-option-group>
              <el-option-group label="热力站"><el-option v-for="s in stations" :key="s.id" :value="s.id" :label="s.name" /></el-option-group>
            </el-select>
          </el-form-item>
          <el-form-item v-if="f.report_type === 'daily'" label="日期">
            <el-date-picker v-model="day" type="date" value-format="YYYY-MM-DD" placeholder="默认昨天" :disabled-date="future" style="width: 150px" />
          </el-form-item>
          <el-form-item v-if="f.report_type === 'weekly' || f.report_type === 'energy'" label="期间">
            <el-date-picker v-model="range" type="daterange" value-format="YYYY-MM-DD" start-placeholder="默认截至昨天 7 天"
                            end-placeholder="" :disabled-date="future" style="width: 260px" />
          </el-form-item>
          <el-form-item v-if="f.report_type === 'incident'" label="事件">
            <el-select v-model="f.event_id" filterable placeholder="选择报警或调控指令" style="width: 380px">
              <el-option-group label="调控指令">
                <el-option v-for="c in commands" :key="c.command_id" :value="c.command_id"
                           :label="`${c.command_id} ${c.station_name} ${c.description}${c.status === 'rolled_back' ? '（已回滚）' : ''}`" />
              </el-option-group>
              <el-option-group label="报警">
                <el-option v-for="a in alarms" :key="a.id + a.first_ts" :value="`${a.id}@${a.first_ts}`"
                           :label="`${a.first_ts.slice(5, 16).replace('T', ' ')} ${a.object_name} ${a.message}`" />
              </el-option-group>
            </el-select>
          </el-form-item>
          <el-form-item><el-button type="primary" :loading="busy" @click="generate">生成</el-button></el-form-item>
        </el-form>
      </div>
      <div class="panel">
        <div class="panel-title">
          <span>报告</span>
          <div class="row">
            <el-select v-model="filter" size="small" clearable placeholder="全部类型" style="width: 120px">
              <el-option v-for="(n, k) in meta?.types ?? {}" :key="k" :value="k" :label="n" />
            </el-select>
            <el-button size="small" @click="load">刷新</el-button>
          </div>
        </div>
        <el-table :data="rows" size="small" empty-text="暂无报告" @row-click="(r: any) => router.push(r.link)" class="clickable">
          <el-table-column prop="title" label="标题" min-width="260" show-overflow-tooltip />
          <el-table-column prop="type_name" label="类型" width="100" />
          <el-table-column label="关键结论" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.key_findings[0] }}</template>
          </el-table-column>
          <el-table-column label="核对" width="90">
            <template #default="{ row }"><el-tag size="small" :type="row.check_ok ? 'success' : 'danger'">{{ row.numbers_checked }} 个数字</el-tag></template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="110" />
          <el-table-column label="生成" width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.generated_at.slice(5, 16).replace('T', ' ') }} {{ row.generated_by }}</template>
          </el-table-column>
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import { api, getAuthToken, getData, qs } from '@/api/http'
import { useAuth } from '@/stores/auth'
import { useRealtime } from '@/stores/realtime'

const auth = useAuth()
const rt = useRealtime()
const route = useRoute()
const router = useRouter()
const meta = ref<any>(null)
const rows = ref<any[]>([])
const filter = ref('')
const stations = computed(() => (rt.snap ? (Object.values(rt.snap.stations) as any[]) : []))
const sources = computed(() => (rt.snap ? (Object.values(rt.snap.sources) as any[]) : []))
const future = (d: Date) => d.getTime() > Date.now()
const cell = (v: any) => (v === null || v === undefined ? '—' : v)

// 生成
const f = reactive({ report_type: 'daily', scope_id: 'NET', event_id: '' })
const day = ref('')
const range = ref<string[] | null>(null)
const busy = ref(false)
const alarms = ref<any[]>([])
const commands = ref<any[]>([])
async function generate() {
  const period = f.report_type === 'daily' ? day.value || null : range.value ? `${range.value[0]}~${range.value[1]}` : null
  busy.value = true
  try {
    const r = await api.post<any>('/api/reports', { ...f, period, event_id: f.event_id || null })
    ElMessage.success(r.summary)
    router.push(r.data.link)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    busy.value = false
  }
}
async function load() {
  rows.value = await getData(`/api/reports${qs({ type: filter.value })}`)
}
watch(filter, load)
watch(() => f.report_type, async (t) => {
  if (t === 'incident' && !commands.value.length) {
    ;[alarms.value, commands.value] = await Promise.all([getData<any[]>('/api/alarms?history=true'), getData<any[]>('/api/control/commands')])
  }
})

// 查看
const rep = ref<any>(null)
const conclusion = ref('')
const factRows = computed(() => (rep.value ? Object.entries(rep.value.facts).map(([key, v]: any) => ({ key, ...v })) : []))
async function loadReport() {
  rep.value = null
  if (route.params.id) rep.value = await getData(`/api/reports/${route.params.id}`)
}
watch(() => route.params.id, (id) => (id ? loadReport() : load()))
async function openHtml() {
  // 打印版需带令牌获取，再以 blob 打开（HTML 已转义并带 CSP，不执行脚本）
  const res = await fetch(`/api/reports/${rep.value.report_id}/html`, { headers: { Authorization: `Bearer ${getAuthToken()}` } })
  if (!res.ok) return ElMessage.error('打开失败')
  const url = URL.createObjectURL(new Blob([await res.text()], { type: 'text/html' }))
  window.open(url, '_blank', 'noopener')
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
async function review(approve: boolean) {
  try {
    const r = await api.post<any>(`/api/reports/${rep.value.report_id}/review`, { approve, conclusion: conclusion.value })
    ElMessage.success(r.summary)
    loadReport()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

function chartOption(c: any): EChartsOption {
  const right = c.series.some((s: any) => s.axis === 'right')
  return <EChartsOption>{
    title: { text: c.title, left: 0, top: 0, textStyle: { fontSize: 12, color: '#c7d6ee' } },
    legend: { top: 0, right: 0 },
    grid: { top: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: c.x, axisLabel: { rotate: c.type === 'bar' && c.x.length > 6 ? 30 : 0 } },
    yAxis: right ? [{ type: 'value', scale: true }, { type: 'value', scale: true }] : { type: 'value', scale: c.type !== 'bar' },
    series: c.series.map((s: any) => ({ name: s.name, type: c.type, data: s.data, yAxisIndex: s.axis === 'right' ? 1 : 0,
      showSymbol: false, smooth: c.type === 'line' })),
  }
}

onMounted(async () => {
  meta.value = await getData('/api/reports/meta')
  if (route.params.id) loadReport()
  else load()
})
</script>

<style scoped>
.top { margin-bottom: 4px; }
.doc { max-width: 1100px; }
.doc h1 { font-size: 20px; margin: 0 0 6px; }
.doc h2 { font-size: 15px; margin: 22px 0 8px; padding-left: 8px; border-left: 3px solid var(--primary, #3b8cff); }
.doc p { font-size: 14px; line-height: 1.85; margin: 6px 0; }
.find li { font-weight: 600; line-height: 1.9; }
.method li { font-size: 12px; color: var(--muted); line-height: 1.8; }
.small { font-size: 12px; }
.mt { margin-top: 8px; }
.facts { margin-top: 16px; font-size: 13px; }
.facts summary { cursor: pointer; color: var(--muted); }
.review { margin-top: 16px; }
.clickable :deep(.el-table__row) { cursor: pointer; }
</style>
