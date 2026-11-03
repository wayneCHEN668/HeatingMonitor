<template>
  <div class="page">
    <el-tabs v-model="tab">
      <!-- ============ 工作台 ============ -->
      <el-tab-pane label="诉求工作台" name="desk">
        <el-row :gutter="12">
          <el-col :lg="11">
            <div class="panel">
              <div class="panel-title">登记诉求</div>
              <el-form label-width="70px" size="small">
                <el-form-item label="用户">
                  <div class="row w100">
                    <el-input v-model="key" placeholder="用户号 / 手机号 / 完整地址" clearable @keyup.enter="lookup(false)" />
                    <el-button @click="lookup(false)">查询</el-button>
                  </div>
                </el-form-item>
                <el-form-item label="渠道">
                  <el-radio-group v-model="channel"><el-radio-button v-for="c in ['热线', '小程序', '微信', '现场']" :key="c" :value="c">{{ c }}</el-radio-button></el-radio-group>
                </el-form-item>
                <el-form-item label="用户原话">
                  <el-input v-model="text" type="textarea" :rows="3" placeholder="原样记录，如：家里暖气不热，屋里只有15度，家里有老人" />
                </el-form-item>
                <el-form-item v-if="auth.has('service:write')">
                  <el-button type="primary" :disabled="text.trim().length < 2" @click="register">登记并分类</el-button>
                  <el-button :disabled="text.trim().length < 2" @click="preview">仅试分类</el-button>
                </el-form-item>
              </el-form>
            </div>

            <div v-if="cls" class="panel">
              <div class="panel-title">
                <span>分类结果 <span v-if="req" class="sub">{{ req.request_id }} · {{ req.status }}{{ req.merged ? ' · 重复来电已合并' : '' }}</span></span>
                <span class="sub">置信度 {{ cls.confidence }}</span>
              </div>
              <div class="tags">
                <el-tag effect="dark" size="large">{{ cls.category }}</el-tag>
                <el-tag v-if="cls.urgent" type="danger" effect="dark" size="large">紧急</el-tag>
                <el-tag v-if="cls.human" type="warning" effect="dark" size="large">转人工</el-tag>
                <el-tag v-else-if="cls.confidence === '低'" type="warning" size="large">送人工复核</el-tag>
                <el-tag v-if="cls.room_temps.length" type="info" size="large">室温 {{ cls.room_temps.join('、') }}℃</el-tag>
              </div>
              <el-alert v-if="cls.injection_flags.length" type="error" :closable="false" show-icon class="mb"
                        title="用户原话含指令性内容，只作记录，不得执行" :description="cls.injection_flags.join('、')" />
              <ul class="reasons">
                <li v-if="cls.urgent_reason" class="danger">{{ cls.urgent_reason }}</li>
                <li v-for="r in cls.human_reasons" :key="r" class="warn">转人工：{{ r }}</li>
                <li v-for="r in cls.reasons" :key="r">{{ r }}</li>
              </ul>
              <div class="muted small">{{ cls.method }}。规则分类器在留出集上类别准确率 65%，结果仅作建议。</div>
              <div v-if="req" class="actions">
                <template v-for="a in req.suggested_actions" :key="a.action">
                  <el-button v-if="a.tool === 'ops_create_workorder'" size="small" type="primary" @click="makeWo(a.args)">
                    创建工单（{{ a.args.priority }}）</el-button>
                  <el-button v-else-if="a.tool === 'service_handoff_to_dispatch'" size="small" type="danger" plain @click="handoff">
                    移交调度：{{ a.why }}</el-button>
                  <el-button v-else-if="a.tool === 'service_draft_reply'" size="small" @click="draft">生成回复草稿</el-button>
                  <el-tag v-else type="warning">{{ a.action }}：{{ a.why }}</el-tag>
                </template>
              </div>
            </div>

            <div v-if="reply" class="panel">
              <div class="panel-title"><span>回复草稿</span><span class="sub">{{ reply.status }}</span></div>
              <el-input v-model="reply.reply" type="textarea" :rows="5" />
              <ul class="reasons"><li v-for="c in reply.cautions" :key="c" class="warn">{{ c }}</li></ul>
              <div class="muted small">依据：<span v-for="c in reply.citations" :key="c.doc_id + c.section">{{ c.doc_id }} {{ c.section }}；</span></div>
              <div class="actions"><el-button size="small" @click="copy(reply.reply)">复制</el-button></div>
            </div>
          </el-col>

          <el-col :lg="13">
            <div class="panel" v-loading="looking">
              <div class="panel-title">
                <span>用户 360</span>
                <el-button v-if="cust && auth.has('customer:pii') && cust.customer.masked" size="small" link type="warning" @click="lookup(true)">
                  显示完整姓名电话（记审计）</el-button>
              </div>
              <template v-if="cust">
                <el-descriptions :column="2" size="small" border>
                  <el-descriptions-item label="用户">{{ cust.customer.name }} · {{ cust.customer.phone }}</el-descriptions-item>
                  <el-descriptions-item label="用户号">{{ cust.customer.customer_id }}</el-descriptions-item>
                  <el-descriptions-item label="地址" :span="2">{{ cust.customer.address }}（{{ cust.customer.position }}，{{ cust.customer.area_m2 }} m²）</el-descriptions-item>
                  <el-descriptions-item label="账户">
                    <el-tag size="small" :type="cust.account.status === '正常' ? 'success' : 'warning'">{{ cust.account.status }}</el-tag>
                    <span v-if="cust.account.arrears_yuan"> 欠费 {{ cust.account.arrears_yuan }} 元</span>
                  </el-descriptions-item>
                  <el-descriptions-item label="所属">{{ cust.station.name }} · {{ cust.station.source }}</el-descriptions-item>
                  <el-descriptions-item label="站运行">
                    <template v-if="cust.station.online">典型室温 {{ fx(cust.station.typical_tin) }}℃ · 二次供/回 {{ fx(cust.station.ts2) }}/{{ fx(cust.station.tr2) }}℃</template>
                    <el-tag v-else type="danger" size="small">通讯中断</el-tag>
                  </el-descriptions-item>
                  <el-descriptions-item label="估算室温">
                    <b :class="{ danger: cust.room_temp_est !== null && cust.room_temp_est < 18 }">{{ cust.room_temp_est ?? '—' }}℃</b>
                    <span class="muted small"> {{ cust.room_temp_note }}</span>
                  </el-descriptions-item>
                </el-descriptions>
                <ul class="reasons"><li v-for="x in cust.facts" :key="x">{{ x }}</li></ul>
                <div class="sub-title">工单（{{ cust.workorders.length }}）</div>
                <el-table :data="cust.workorders" size="small" empty-text="无">
                  <el-table-column prop="workorder_id" label="工单" width="120" />
                  <el-table-column prop="type" label="类型" width="80" />
                  <el-table-column prop="status" label="状态" width="80" />
                  <el-table-column prop="assignee_name" label="处理人" width="80" />
                  <el-table-column label="创建"><template #default="{ row }">{{ tm(row.created_at) }}</template></el-table-column>
                </el-table>
                <div class="sub-title">诉求历史（{{ cust.requests.length }}）</div>
                <el-table :data="cust.requests" size="small" empty-text="无">
                  <el-table-column prop="request_id" label="诉求" width="120" />
                  <el-table-column prop="category" label="类别" width="80" />
                  <el-table-column prop="text" label="内容" show-overflow-tooltip />
                  <el-table-column prop="status" label="状态" width="80" />
                </el-table>
              </template>
              <el-empty v-else description="输入用户号、手机号或地址查询" :image-size="60" />
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- ============ 诉求列表 ============ -->
      <el-tab-pane label="诉求列表" name="list">
        <div class="grid kpis" v-if="list">
          <KpiCard label="近 7 天诉求" :value="list.total" />
          <KpiCard label="紧急" :value="list.urgent" tone="danger" />
          <KpiCard label="转人工" :value="list.human" tone="warn" />
          <KpiCard label="待人工" :value="list.by_status['待人工'] ?? 0" tone="warn" />
        </div>
        <div class="panel">
          <div class="panel-title"><span>诉求</span><el-button size="small" @click="loadList">刷新</el-button></div>
          <el-table :data="list?.requests ?? []" size="small" empty-text="暂无诉求">
            <el-table-column prop="request_id" label="诉求" width="120" />
            <el-table-column label="类别" width="140"><template #default="{ row }">
              {{ row.classification.category }}
              <el-tag v-if="row.classification.urgent" size="small" type="danger">紧急</el-tag>
              <el-tag v-if="row.classification.human" size="small" type="warning">人工</el-tag>
            </template></el-table-column>
            <el-table-column prop="text" label="用户原话" min-width="240" show-overflow-tooltip />
            <el-table-column prop="channel" label="渠道" width="70" />
            <el-table-column prop="status" label="状态" width="80" />
            <el-table-column label="工单/移交" width="200"><template #default="{ row }">{{ [row.workorder_id, row.handoff_id].filter(Boolean).join(' / ') }}</template></el-table-column>
            <el-table-column prop="created_by" label="登记人" width="140" show-overflow-tooltip />
            <el-table-column label="时间" width="120"><template #default="{ row }">{{ tm(row.created_at) }}</template></el-table-column>
            <el-table-column v-if="auth.has('service:write')" label="" width="70">
              <template #default="{ row }"><el-button v-if="row.status !== '已办结'" size="small" link @click="closeReq(row)">办结</el-button></template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ============ 通知 ============ -->
      <el-tab-pane :label="`用户通知${pendingNotices ? `（${pendingNotices} 待确认）` : ''}`" name="notice">
        <div v-if="auth.has('service:notify')" class="panel">
          <div class="panel-title"><span>新建通知 <span class="sub">报停户不接收；缴费提醒只发欠费户；计划停热须提前 24 小时（SVC-01 §5）</span></span></div>
          <el-form inline size="small">
            <el-form-item label="类型">
              <el-select v-model="nf.kind" style="width: 110px"><el-option v-for="k in meta?.notice_kinds ?? []" :key="k" :value="k" :label="k" /></el-select>
            </el-form-item>
            <el-form-item label="范围">
              <el-select v-model="nf.scope_id" filterable style="width: 200px">
                <el-option-group label="热源"><el-option v-for="s in sources" :key="s.id" :value="s.id" :label="s.name" /></el-option-group>
                <el-option-group label="热力站"><el-option v-for="s in stations" :key="s.id" :value="s.id" :label="s.name" /></el-option-group>
                <el-option-group label="楼栋"><el-option v-for="b in meta?.buildings ?? []" :key="b.building_id" :value="b.building_id" :label="b.name" /></el-option-group>
              </el-select>
            </el-form-item>
            <el-form-item v-if="nf.kind !== '缴费提醒'" label="原因"><el-input v-model="nf.reason" style="width: 160px" /></el-form-item>
            <el-form-item v-if="nf.kind === '停热' || nf.kind === '恢复供热'" label="开始">
              <el-date-picker v-model="nf.start" type="datetime" value-format="YYYY-MM-DDTHH:mm" style="width: 180px" /></el-form-item>
            <el-form-item v-if="nf.kind === '停热' || nf.kind === '抢修'" label="预计恢复">
              <el-date-picker v-model="nf.end" type="datetime" value-format="YYYY-MM-DDTHH:mm" style="width: 180px" /></el-form-item>
            <el-form-item><el-button type="primary" @click="createNotice">生成（待确认）</el-button></el-form-item>
          </el-form>
        </div>
        <div class="panel">
          <div class="panel-title"><span>通知记录</span><el-button size="small" @click="loadNotices">刷新</el-button></div>
          <el-table :data="notices" size="small" empty-text="暂无通知">
            <el-table-column prop="notice_id" label="编号" width="110" />
            <el-table-column prop="kind" label="类型" width="80" />
            <el-table-column prop="scope_name" label="范围" width="140" />
            <el-table-column prop="content" label="内容" min-width="280" show-overflow-tooltip />
            <el-table-column prop="recipients" label="户数" width="64" />
            <el-table-column label="发起" width="150"><template #default="{ row }">
              {{ row.created_by }}<el-tag v-if="row.actor_kind === 'agent'" size="small" type="info">智能体</el-tag></template></el-table-column>
            <el-table-column label="状态" width="230"><template #default="{ row }">
              <el-tag size="small" :type="row.status === '已发送' ? 'success' : row.status === '待确认' ? 'warning' : 'info'">{{ row.status }}</el-tag>
              <span v-if="row.delivery" class="small"> 送达 {{ row.delivery.delivered }} / 失败 {{ row.delivery.failed }}</span>
              <template v-if="row.status === '待确认' && auth.has('service:notify')">
                <el-button size="small" link type="primary" @click="confirmNotice(row, true)">确认发送</el-button>
                <el-button size="small" link @click="confirmNotice(row, false)">驳回</el-button>
              </template>
              <div v-for="w in row.warnings" :key="w" class="warn small">{{ w }}</div>
            </template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>
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
const tab = ref('desk')
const tm = (s?: string | null) => (s ? s.slice(5, 16).replace('T', ' ') : '')
const fx = (v?: number | null) => (v === null || v === undefined ? '—' : v.toFixed(1))
const meta = ref<any>(null)
const stations = computed(() => (rt.snap ? (Object.values(rt.snap.stations) as any[]) : []))
const sources = computed(() => (rt.snap ? (Object.values(rt.snap.sources) as any[]) : []))

// 用户 360
const key = ref('')
const cust = ref<any>(null)
const looking = ref(false)
async function lookup(reveal: boolean) {
  if (!key.value.trim() && !cust.value) return
  looking.value = true
  try {
    cust.value = await getData(`/api/service/customers/lookup${qs({ key: reveal ? cust.value.customer.customer_id : key.value.trim(), reveal: reveal ? 'true' : '' })}`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    looking.value = false
  }
}

// 诉求
const channel = ref('热线')
const text = ref('')
const cls = ref<any>(null)
const req = ref<any>(null)
const reply = ref<any>(null)
async function preview() {
  cls.value = (await api.post<any>('/api/service/classify', { text: text.value })).data
  req.value = null
}
async function register() {
  try {
    const r = await api.post<any>('/api/service/requests', { text: text.value, channel: channel.value, customer_key: cust.value?.customer.customer_id ?? (key.value.trim() || null) })
    req.value = r.data
    cls.value = r.data.classification
    reply.value = null
    ElMessage.success(r.summary)
    r.warnings.forEach((w: string) => ElMessage.warning(w))
    if (req.value.customer_id) lookupById(req.value.customer_id)
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function lookupById(id: string) {
  cust.value = await getData(`/api/service/customers/lookup${qs({ key: id })}`)
}
async function makeWo(args: any) {
  if (!args.customer_id) return ElMessage.warning('诉求未关联用户，请先查询并核实用户')
  try {
    const r = await api.post<any>('/api/ops/workorders', { type: args.type, priority: args.priority, customer_id: args.customer_id,
      description: req.value.text, request_id: req.value.request_id })
    ElMessage[r.data.created ? 'success' : 'warning'](r.summary)
    req.value.workorder_id = r.data.workorder_id
    lookupById(args.customer_id)
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function handoff() {
  try {
    const r = await api.post<any>(`/api/service/requests/${req.value.request_id}/handoff`, {})
    ElMessage.success(r.summary)
    req.value.handoff_id = r.data.handoff_id
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function draft() {
  try {
    reply.value = (await api.post<any>(`/api/service/requests/${req.value.request_id}/reply-draft`)).data
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
function copy(s: string) {
  navigator.clipboard?.writeText(s).then(() => ElMessage.success('已复制'), () => ElMessage.warning('复制失败，请手动选择'))
}

// 列表
const list = ref<any>(null)
async function loadList() {
  list.value = await getData('/api/service/requests')
}
async function closeReq(row: any) {
  const { value } = await ElMessageBox.prompt('处理结果', `办结 ${row.request_id}`, { inputValidator: (v) => (v?.trim().length ?? 0) >= 2 || '请填写处理结果' })
  try {
    await api.post(`/api/service/requests/${row.request_id}/close`, { note: value })
    loadList()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

// 通知
const notices = ref<any[]>([])
const pendingNotices = computed(() => notices.value.filter((n) => n.status === '待确认').length)
const nf = reactive({ kind: '抢修', scope_id: '', reason: '', start: '', end: '' })
async function loadNotices() {
  notices.value = await getData('/api/service/notices')
}
async function createNotice() {
  try {
    const r = await api.post<any>('/api/service/notices', { ...nf, start: nf.start || null, end: nf.end || null })
    ElMessage[r.data.duplicate ? 'warning' : 'success'](r.summary)
    r.warnings.forEach((w: string) => ElMessage.warning(w))
    loadNotices()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function confirmNotice(row: any, approve: boolean) {
  if (approve) await ElMessageBox.confirm(`向 ${row.recipients} 户发送：\n${row.content}`, '确认发送', { type: 'warning' })
  try {
    const r = await api.post<any>(`/api/service/notices/${row.notice_id}/confirm`, { approve })
    ElMessage.success(r.summary)
    loadNotices()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

watch(tab, (v) => {
  if (v === 'list') loadList()
  if (v === 'notice') loadNotices()
})
onMounted(async () => {
  meta.value = await getData('/api/ops/meta')
  loadNotices()
})
</script>

<style scoped>
.w100 { width: 100%; }
.row :deep(.el-input) { flex: 1; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.reasons { margin: 8px 0; padding-left: 18px; font-size: 13px; line-height: 1.8; }
.danger { color: var(--danger); }
.warn { color: #f5c56b; }
.small { font-size: 12px; }
.mb { margin-bottom: 8px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.sub-title { font-size: 13px; font-weight: 600; color: #c7d6ee; margin: 12px 0 6px; }
.kpis { grid-template-columns: repeat(4, 1fr); margin-bottom: 12px; }
.panel + .panel { margin-top: 12px; }
@media (max-width: 760px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
</style>
