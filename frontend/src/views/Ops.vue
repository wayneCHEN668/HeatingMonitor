<template>
  <div class="page">
    <el-tabs v-model="tab" class="tabs">
      <!-- ============ 工单 ============ -->
      <el-tab-pane label="工单" name="wo">
        <div class="grid kpis" v-if="wo">
          <KpiCard label="当前筛选" :value="wo.total" :sub="`近 ${wo.days} 天 · ${f.status || '全部状态'}`" />
          <KpiCard label="未完成" :value="openCount" tone="warn" />
          <KpiCard label="超时" :value="wo.overdue" tone="danger" />
          <KpiCard label="整楼不热" :value="wo.clusters.filter((c: any) => c.whole_building).length" tone="danger" />
        </div>
        <el-alert v-for="c in (wo?.clusters ?? []).filter((c: any) => c.whole_building)" :key="c.object_id" type="error" show-icon
                  :closable="false" class="mb" :title="`${c.object_name}：24 小时内 ${c.last_24h} 户报不热`" :description="c.hint" />
        <div class="panel">
          <div class="panel-title">
            <span>工单列表</span>
            <div class="row">
              <el-select v-model="f.status" size="small" clearable placeholder="状态" style="width: 110px">
                <el-option v-for="s in ['未完成', '待派单', '已派单', '处理中', '已完成', '已关闭']" :key="s" :value="s" :label="s" />
              </el-select>
              <el-select v-model="f.type" size="small" clearable placeholder="类型" style="width: 110px">
                <el-option v-for="t in meta?.types ?? []" :key="t" :value="t" :label="t" />
              </el-select>
              <el-select v-model="f.days" size="small" style="width: 100px">
                <el-option v-for="d in [1, 3, 7, 14, 30]" :key="d" :value="d" :label="`近 ${d} 天`" />
              </el-select>
              <el-button v-if="auth.has('ops:write')" size="small" type="primary" @click="createOpen = true">新建工单</el-button>
            </div>
          </div>
          <el-table :data="wo?.workorders ?? []" size="small" max-height="520" v-loading="!wo" @row-click="openWo">
            <el-table-column prop="workorder_id" label="工单号" width="120" />
            <el-table-column label="对象" min-width="150"><template #default="{ row }">{{ row.building_name ?? row.station_name }}</template></el-table-column>
            <el-table-column prop="type" label="类型" width="90" />
            <el-table-column label="优先级" width="76"><template #default="{ row }">
              <el-tag size="small" :type="PRIO[row.priority]" effect="dark">{{ row.priority }}</el-tag></template></el-table-column>
            <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
            <el-table-column label="状态" width="90"><template #default="{ row }">
              <el-tag size="small" :type="row.overdue ? 'danger' : row.status === '已完成' ? 'success' : 'info'">{{ row.overdue ? '超时' : row.status }}</el-tag>
            </template></el-table-column>
            <el-table-column prop="assignee_name" label="处理人" width="86" />
            <el-table-column label="创建" width="128"><template #default="{ row }">{{ t(row.created_at) }}</template></el-table-column>
            <el-table-column label="到场时限" width="128"><template #default="{ row }">{{ t(row.due_at) }}</template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ============ 巡检计划 ============ -->
      <el-tab-pane label="巡检计划" name="patrol">
        <div class="panel">
          <div class="panel-title">
            <span>生成巡检计划 <span class="sub">风险分 = 报警（一/二/三级 3/2/1）+ 异常检测 3 + 未完成工单 + 熔断 2 + 距上次巡检天数/7</span></span>
            <div class="row">
              <el-select v-model="pf.object_id" size="small" style="width: 130px">
                <el-option value="NET" label="全部片区" />
                <el-option v-for="s in rt.snap ? Object.values(rt.snap.sources) : []" :key="(s as any).id" :value="(s as any).id" :label="(s as any).name" />
              </el-select>
              <el-date-picker v-model="pf.date" size="small" type="date" value-format="YYYY-MM-DD" placeholder="今天" style="width: 140px" />
              <el-radio-group v-model="pf.focus" size="small">
                <el-radio-button value="风险优先">风险优先</el-radio-button>
                <el-radio-button value="常规">常规</el-radio-button>
              </el-radio-group>
              <el-button size="small" type="primary" :loading="busy" @click="plan">生成</el-button>
            </div>
          </div>
          <template v-if="patrol">
            <div class="muted small mb">从 {{ patrol.start }} 出发 · {{ patrol.route.length }} 站 · 约 {{ patrol.distance_km }} km · 预计
              {{ patrol.est_hours }} 小时<span v-if="patrol.skipped.length">；未排入：{{ patrol.skipped.join('、') }}</span></div>
            <el-timeline>
              <el-timeline-item v-for="(s, i) in patrol.route" :key="s.station_id" :type="s.risk_score >= 5 ? 'danger' : s.risk_score >= 2 ? 'warning' : 'primary'"
                                :timestamp="`第 ${i + 1} 站 · 路程 ${s.leg_km} km · 风险分 ${s.risk_score}`" placement="top">
                <div class="stop">
                  <b>{{ s.station_name }}</b>
                  <div class="basis"><el-tag v-for="b in s.basis" :key="b" size="small" effect="plain">{{ b }}</el-tag></div>
                  <ul><li v-for="m in s.must_check" :key="m">{{ m }}</li></ul>
                </div>
              </el-timeline-item>
            </el-timeline>
          </template>
          <el-empty v-else description="选择范围后生成" :image-size="60" />
        </div>
      </el-tab-pane>

      <!-- ============ 交接班 ============ -->
      <el-tab-pane label="交接班" name="handover">
        <div class="panel" v-loading="!handover">
          <div class="panel-title">
            <span>{{ handover?.shift_id }} 交接班记录 <span class="sub">{{ t(handover?.start) }} — {{ t(handover?.end) }}</span></span>
            <div class="row">
              <el-input v-model="shift" size="small" placeholder="YYYY-MM-DD-D / -N，留空为当前班" style="width: 230px" />
              <el-button size="small" @click="loadHandover">生成</el-button>
            </div>
          </div>
          <template v-if="handover">
            <p class="summary">{{ handover.summary }}</p>
            <div class="sub-title">接班注意事项</div>
            <ul class="notes"><li v-for="n in handover.notes" :key="n">{{ n }}</li></ul>
            <div class="muted small">{{ handover.weather }}</div>
            <el-row :gutter="12" class="mt">
              <el-col :md="12">
                <div class="sub-title">本班报警（{{ handover.alarms.length }}）</div>
                <el-table :data="handover.alarms" size="small" max-height="260" empty-text="无">
                  <el-table-column prop="object_name" label="对象" width="110" />
                  <el-table-column prop="level" label="级别" width="56" />
                  <el-table-column prop="message" label="内容" show-overflow-tooltip />
                  <el-table-column label="状态" width="70"><template #default="{ row }">{{ row.active ? '未恢复' : '已恢复' }}</template></el-table-column>
                </el-table>
              </el-col>
              <el-col :md="12">
                <div class="sub-title">本班调控（{{ handover.commands.length }}）</div>
                <el-table :data="handover.commands" size="small" max-height="260" empty-text="无">
                  <el-table-column prop="station_name" label="站" width="110" />
                  <el-table-column prop="description" label="指令" show-overflow-tooltip />
                  <el-table-column prop="executed_by" label="下发人" width="120" show-overflow-tooltip />
                  <el-table-column prop="status" label="状态" width="90" />
                </el-table>
              </el-col>
            </el-row>
            <el-row :gutter="12" class="mt">
              <el-col :md="12">
                <div class="sub-title">超时工单（{{ handover.overdue.length }}）</div>
                <el-table :data="handover.overdue" size="small" empty-text="无">
                  <el-table-column prop="workorder_id" label="工单" width="120" />
                  <el-table-column prop="station_name" label="站" width="110" />
                  <el-table-column prop="type" label="类型" width="80" />
                  <el-table-column label="时限"><template #default="{ row }">{{ t(row.due_at) }}</template></el-table-column>
                </el-table>
              </el-col>
              <el-col :md="12">
                <div class="sub-title">本班工单：新建 {{ handover.workorders_created.length }} · 完结 {{ handover.workorders_closed.length }} · 客服诉求 {{ handover.service_requests }}</div>
                <el-table :data="handover.workorders_closed" size="small" empty-text="本班无完结工单">
                  <el-table-column prop="workorder_id" label="工单" width="120" />
                  <el-table-column prop="station_name" label="站" width="110" />
                  <el-table-column prop="result" label="结果" show-overflow-tooltip />
                </el-table>
              </el-col>
            </el-row>
          </template>
        </div>
      </el-tab-pane>

      <!-- ============ 客服移交 ============ -->
      <el-tab-pane :label="`客服移交${openHandoffs ? `（${openHandoffs}）` : ''}`" name="handoff">
        <div class="panel">
          <div class="panel-title"><span>客服移交给调度的整楼/片区问题</span><el-button size="small" @click="loadHandoffs">刷新</el-button></div>
          <el-table :data="handoffs" size="small" empty-text="暂无移交">
            <el-table-column prop="handoff_id" label="编号" width="96" />
            <el-table-column label="对象" width="160"><template #default="{ row }">{{ row.building_name ?? row.station_name }}</template></el-table-column>
            <el-table-column prop="summary" label="说明" min-width="240" />
            <el-table-column label="关联诉求" width="80"><template #default="{ row }">{{ row.related_requests.length }}</template></el-table-column>
            <el-table-column prop="from" label="移交人" width="140" show-overflow-tooltip />
            <el-table-column label="时间" width="128"><template #default="{ row }">{{ t(row.created_at) }}</template></el-table-column>
            <el-table-column label="状态" width="210">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === '已关闭' ? 'success' : row.status === '待接收' ? 'danger' : 'warning'">{{ row.status }}</el-tag>
                <template v-if="auth.has('ops:write') && row.status !== '已关闭'">
                  <el-button v-if="row.status === '待接收'" size="small" link type="primary" @click="handoffAct(row, '已接收')">接收</el-button>
                  <el-button size="small" link type="success" @click="handoffAct(row, '已关闭')">关闭</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 工单详情 -->
    <el-drawer v-model="woOpen" :title="cur ? `工单 ${cur.workorder_id}` : ''" size="520px">
      <template v-if="cur">
        <div class="kv"><span>对象</span><span>{{ cur.building_name ?? cur.station_name }}（{{ cur.station_name }}）</span></div>
        <div class="kv"><span>类型 / 优先级</span><span>{{ cur.type }} / {{ cur.priority }}</span></div>
        <div class="kv"><span>状态</span><span>{{ cur.status }}{{ cur.overdue ? '（已超时）' : '' }}</span></div>
        <div class="kv"><span>到场时限</span><span>{{ t(cur.due_at) }}</span></div>
        <div class="kv"><span>处理人</span><span>{{ cur.assignee_name ?? '未派单' }}</span></div>
        <div class="kv"><span>来源</span><span>{{ cur.source }} · {{ cur.created_by }}</span></div>
        <p class="desc">{{ cur.description }}</p>
        <el-alert v-if="cur.flags?.length" type="warning" :closable="false" show-icon title="描述含指令性文字，仅作为描述保存" :description="cur.flags.join('、')" />
        <template v-if="sug">
          <div class="sub-title">派单推荐 <span class="muted small">{{ sug.method }}</span></div>
          <div v-for="c in sug.candidates" :key="c.staff_id" class="cand">
            <div><b>{{ c.name }}</b> <span class="muted">{{ c.team }} · {{ c.skills.join('、') }}</span></div>
            <div class="muted small">{{ c.reasons.join('；') }} · 评分 {{ c.score }}</div>
            <el-button v-if="auth.has('ops:write') && !['已完成', '已关闭'].includes(cur.status)" size="small" @click="assign(c.staff_id)">派给他</el-button>
          </div>
        </template>
        <div v-if="auth.has('ops:write') && !['已完成', '已关闭'].includes(cur.status)" class="ops">
          <el-input v-model="result" size="small" placeholder="处理结果（完成/关闭时必填）" />
          <el-button v-if="cur.status === '已派单'" size="small" @click="setStatus('处理中')">开始处理</el-button>
          <el-button size="small" type="success" @click="setStatus('已完成')">完成</el-button>
          <el-button size="small" @click="setStatus('已关闭')">关闭</el-button>
        </div>
        <div class="sub-title">记录</div>
        <div v-for="(l, i) in cur.log" :key="i" class="small log"><span class="muted">{{ t(l.ts) }}</span> {{ l.by }}：{{ l.action }}</div>
        <div v-if="cur.result" class="small">结果：{{ cur.result }}</div>
      </template>
    </el-drawer>

    <!-- 新建工单 -->
    <el-dialog v-model="createOpen" title="新建工单" width="480px">
      <el-form label-width="80px" size="small">
        <el-form-item label="对象">
          <el-select v-model="nf.object_id" filterable placeholder="楼栋或热力站" style="width: 100%">
            <el-option-group label="热力站">
              <el-option v-for="s in stations" :key="s.id" :value="s.id" :label="s.name" />
            </el-option-group>
            <el-option-group label="楼栋">
              <el-option v-for="b in meta?.buildings ?? []" :key="b.building_id" :value="b.building_id" :label="b.name" />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="住户号"><el-input v-model="nf.customer_id" placeholder="住户工单填写（可选）" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="nf.type" style="width: 100%"><el-option v-for="t in meta?.types ?? []" :key="t" :value="t" :label="t" /></el-select>
        </el-form-item>
        <el-form-item label="优先级">
          <el-radio-group v-model="nf.priority">
            <el-radio-button v-for="(h, p) in meta?.priorities ?? {}" :key="p" :value="p">{{ p }}（{{ h }}h）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="描述"><el-input v-model="nf.description" type="textarea" :rows="3" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import KpiCard from '@/components/KpiCard.vue'
import { api, getData, qs } from '@/api/http'
import { useAuth } from '@/stores/auth'
import { useRealtime } from '@/stores/realtime'

const auth = useAuth()
const rt = useRealtime()
const tab = ref('wo')
const PRIO: Record<string, string> = { 紧急: 'danger', 高: 'warning', 普通: 'info', 低: 'info' }
const t = (s?: string | null) => (s ? s.slice(5, 16).replace('T', ' ') : '')

const meta = ref<any>(null)
const wo = ref<any>(null)
const f = reactive({ status: '未完成', type: '', days: 7 })
const openCount = computed(() => (wo.value ? Object.entries(wo.value.by_status).filter(([k]) => !['已完成', '已关闭'].includes(k))
  .reduce((a, [, v]) => a + (v as number), 0) : 0))
const stations = computed(() => (rt.snap ? Object.values(rt.snap.stations) as any[] : []))

async function loadWo() {
  wo.value = await getData(`/api/ops/workorders${qs({ status: f.status, type: f.type, days: f.days })}`)
}
watch(f, loadWo)

// 工单详情
const woOpen = ref(false)
const cur = ref<any>(null)
const sug = ref<any>(null)
const result = ref('')
async function openWo(row: any) {
  cur.value = await getData(`/api/ops/workorders/${row.workorder_id}`)
  sug.value = null
  result.value = ''
  woOpen.value = true
  if (!['已完成', '已关闭'].includes(cur.value.status)) sug.value = await getData(`/api/ops/workorders/${row.workorder_id}/suggest`)
}
async function assign(staff: string) {
  try {
    const r = await api.post<any>(`/api/ops/workorders/${cur.value.workorder_id}/assign`, { staff_id: staff })
    cur.value = r.data
    ElMessage.success(r.summary)
    loadWo()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function setStatus(status: string) {
  try {
    const r = await api.post<any>(`/api/ops/workorders/${cur.value.workorder_id}/status`, { status, result: result.value })
    cur.value = r.data
    ElMessage.success(r.summary)
    loadWo()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

// 新建工单
const createOpen = ref(false)
const nf = reactive({ object_id: '', customer_id: '', type: '巡检', priority: '普通', description: '' })
async function create() {
  try {
    const r = await api.post<any>('/api/ops/workorders', { ...nf, customer_id: nf.customer_id.trim() || null })
    ElMessage[r.data.created ? 'success' : 'warning'](r.summary)
    createOpen.value = false
    nf.description = ''
    loadWo()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

// 巡检
const pf = reactive({ object_id: 'NET', date: '', focus: '风险优先' })
const patrol = ref<any>(null)
const busy = ref(false)
async function plan() {
  busy.value = true
  try {
    patrol.value = (await api.post<any>('/api/ops/patrol-plan', { ...pf, date: pf.date || null })).data
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    busy.value = false
  }
}

// 交接班
const shift = ref('')
const handover = ref<any>(null)
async function loadHandover() {
  try {
    handover.value = await getData(`/api/ops/handover${qs({ shift_id: shift.value.trim() })}`)
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

// 移交
const handoffs = ref<any[]>([])
const openHandoffs = computed(() => handoffs.value.filter((h) => h.status === '待接收').length)
async function loadHandoffs() {
  handoffs.value = await getData('/api/ops/handoffs')
}
async function handoffAct(row: any, status: string) {
  let note = ''
  if (status === '已关闭') {
    const r = await ElMessageBox.prompt('处理结果', `关闭移交 ${row.handoff_id}`, { inputValidator: (v) => (v?.trim().length ?? 0) >= 2 || '请填写处理结果' })
    note = r.value
  }
  try {
    const r = await api.post<any>(`/api/ops/handoffs/${row.handoff_id}/status`, { status, result: note })
    ElMessage.success(r.summary)
    loadHandoffs()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

watch(tab, (v) => {
  if (v === 'handover' && !handover.value) loadHandover()
  if (v === 'patrol' && !patrol.value) plan()
  if (v === 'handoff') loadHandoffs()
})
onMounted(async () => {
  meta.value = await getData('/api/ops/meta')
  loadWo()
  loadHandoffs()
})
</script>

<style scoped>
.kpis { grid-template-columns: repeat(4, 1fr); margin-bottom: 12px; }
.mb { margin-bottom: 10px; }
.mt { margin-top: 12px; }
.small { font-size: 12px; }
.sub-title { font-size: 13px; font-weight: 600; color: #c7d6ee; margin: 12px 0 6px; }
.summary { font-size: 14px; line-height: 1.8; margin: 0 0 6px; }
.notes { margin: 0; padding-left: 20px; line-height: 1.9; font-size: 13px; color: #f5c56b; }
.stop ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; line-height: 1.8; color: #c7d6ee; }
.basis { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.desc { font-size: 13px; line-height: 1.7; background: #0b1426; padding: 8px 10px; border-radius: 4px; }
.cand { border: 1px solid var(--border); border-radius: 4px; padding: 8px; margin-bottom: 6px; display: grid; gap: 4px; }
.cand .el-button { justify-self: start; }
.ops { display: flex; gap: 6px; margin: 12px 0; }
.log { padding: 2px 0; }
@media (max-width: 760px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
</style>
