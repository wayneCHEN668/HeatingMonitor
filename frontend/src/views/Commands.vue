<template>
  <div class="page">
    <el-tabs v-model="tab">
      <el-tab-pane v-if="auth.hasAny('control:draft', 'control:approve')" name="approve">
        <template #label>待审批 <el-badge v-if="pending.length" :value="pending.length" class="tab-badge" /></template>
        <div class="panel bar">
          <template v-if="auth.has('control:approve')">
            <span class="muted">审批人：<b>{{ auth.me?.label }}</b></span>
            <span class="muted small">智能体与调度员只能草拟和提交审批；批准后系统签发一次性令牌，由申请方凭令牌下发。</span>
          </template>
          <span v-else class="muted small">以下为待值班调度长审批的请求（只读）。你提交的请求批准后，可在“草案”页下发。</span>
        </div>
        <el-empty v-if="!pending.length" description="暂无待审批的调控请求" />
        <div v-for="d in pending" :key="d.draft_id" class="panel req">
          <div class="req-head">
            <el-tag size="small" :type="d.source === 'ai' ? 'success' : 'info'" effect="dark">{{ d.source === 'ai' ? 'AI' : '人工' }}</el-tag>
            <b>{{ d.station_name }}</b> · {{ d.param_text }}
            <span class="muted">{{ d.approval_request?.approval_id }} · 草案 {{ d.draft_id }}</span>
            <span class="muted ml-auto">申请人 {{ d.approval_request?.requested_by }} · {{ d.approval_request?.ts.slice(11) }} · 有效至 {{ d.approval_request?.expires_at.slice(11) }}</span>
          </div>
          <div class="desc">{{ d.description }}</div>
          <div v-if="d.reason || d.approval_request?.reason" class="reason">
            <span class="muted">理由：</span>{{ [d.reason, d.approval_request?.reason].filter(Boolean).join('；') }}
          </div>
          <div class="grid two">
            <table class="pred">
              <thead><tr><th>仿真预测</th><th>二次供温</th><th>阀门</th><th>热功率</th><th>室温</th></tr></thead>
              <tbody>
                <tr><td class="muted">调整前</td><td>{{ fmt(d.expected.before.ts2) }}℃</td><td>{{ fmt(d.expected.before.valve) }}%</td>
                  <td>{{ fmt(d.expected.before.heat_mw, 2) }}MW</td><td>{{ fmt(d.expected.before.tin) }}℃</td></tr>
                <tr><td>调整后</td><td>{{ fmt(d.expected.after.ts2) }}℃</td><td>{{ fmt(d.expected.after.valve) }}%</td>
                  <td>{{ fmt(d.expected.after.heat_mw, 2) }}MW</td>
                  <td :class="d.expected.after.tin < 18 ? 'danger' : d.expected.after.tin > 24 ? 'warn' : 'ok'">{{ fmt(d.expected.after.tin) }}℃</td></tr>
              </tbody>
            </table>
            <div class="checks" v-if="d.check">
              <div class="verdict" :class="d.check.verdict">安全校验：{{ ({ pass: '通过', warn: '通过（有告警）', block: '拦截' } as any)[d.check.verdict] }}</div>
              <div v-for="it in d.check.items" :key="it.name" class="ck">
                <span :class="it.status">{{ it.status === 'pass' ? '✓' : it.status === 'warn' ? '!' : '✕' }}</span>
                <span class="muted nm">{{ it.name }}</span>{{ it.message }}
              </div>
            </div>
          </div>
          <div v-if="auth.has('control:approve')" class="ops">
            <el-input v-model="comments[d.draft_id]" size="small" placeholder="审批意见（可选）" style="max-width: 360px" />
            <el-button size="small" type="danger" plain @click="decide(d, false)">驳回</el-button>
            <el-button size="small" type="primary" @click="decide(d, true)">批准并签发令牌</el-button>
            <el-button size="small" link @click="router.push(`/stations/${d.station_id}`)">查看站点 →</el-button>
          </div>
        </div>
      </el-tab-pane>
      <el-tab-pane name="breaker">
        <template #label>熔断 <el-badge v-if="tripped" :value="tripped" type="danger" class="tab-badge" /></template>
        <BreakerPanel v-if="tab === 'breaker'" />
      </el-tab-pane>
      <el-tab-pane label="指令记录" name="cmd">
        <div class="panel">
          <el-table :data="cmds" size="small" empty-text="暂无下发记录（可在热力站详情页、AI 曲线优化或通过 MCP 智能体下发）">
            <el-table-column prop="command_id" label="指令号" width="120" />
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.executed_at.replace('T', ' ') }}</template></el-table-column>
            <el-table-column prop="station_name" label="站点" width="110" />
            <el-table-column prop="param_text" label="参数" width="110" />
            <el-table-column prop="description" label="内容" min-width="240" show-overflow-tooltip />
            <el-table-column label="来源" width="70"><template #default="{ row }"><el-tag size="small" :type="row.source === 'ai' ? 'success' : 'info'">{{ row.source === 'ai' ? 'AI' : '人工' }}</el-tag></template></el-table-column>
            <el-table-column prop="executed_by" label="执行人" width="170" show-overflow-tooltip />
            <el-table-column prop="approver" label="审批" width="130" />
            <el-table-column label="回执" width="80"><template #default="{ row }">{{ row.ack.latency_ms }}ms</template></el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }"><el-tag size="small" :type="row.status === 'executed' ? 'success' : 'info'">{{ row.status === 'executed' ? '已执行' : '已回滚' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }"><el-button v-if="row.status === 'executed' && auth.has('control:execute')" size="small" link type="danger" @click="rollback(row)">回滚</el-button></template>
            </el-table-column>
            <el-table-column v-if="auth.has('kb:write')" label="" width="96">
              <template #default="{ row }"><el-button size="small" link @click="caseCmd = row; caseOpen = true">沉淀为案例</el-button></template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane v-if="auth.hasAny('control:draft', 'control:approve')" label="草案" name="draft">
        <div class="panel">
          <el-table :data="drafts" size="small">
            <el-table-column prop="draft_id" label="草案号" width="110" />
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.created_at.replace('T', ' ') }}</template></el-table-column>
            <el-table-column prop="station_name" label="站点" width="110" />
            <el-table-column prop="description" label="内容" min-width="240" show-overflow-tooltip />
            <el-table-column prop="created_by" label="创建人" width="170" show-overflow-tooltip />
            <el-table-column label="校验" width="90"><template #default="{ row }">
              <el-tag v-if="row.check" size="small" :type="({ pass: 'success', warn: 'warning', block: 'danger' } as any)[row.check.verdict]">{{ row.check.verdict }}</el-tag>
            </template></el-table-column>
            <el-table-column label="状态" width="100"><template #default="{ row }">{{ STATUS[row.status] ?? row.status }}</template></el-table-column>
            <el-table-column label="审批人" width="150" show-overflow-tooltip><template #default="{ row }">{{ row.approval?.approver }}</template></el-table-column>
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-button v-if="row.status === 'approved' && row.approval?.token && auth.has('control:execute')"
                           size="small" link type="warning" @click="execute(row)">下发</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane v-if="auth.has('audit:read')" label="审计日志" name="audit">
        <div class="panel">
          <el-radio-group v-model="auditFilter" size="small" class="mb">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="tool_call">智能体工具调用</el-radio-button>
            <el-radio-button value="control">调控操作</el-radio-button>
          </el-radio-group>
          <el-table :data="auditRows" size="small">
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.ts.replace('T', ' ') }}</template></el-table-column>
            <el-table-column label="动作" width="190">
              <template #default="{ row }">{{ row.action === 'tool_call' ? `工具 ${row.detail.tool}` : row.action }}
                <el-tag v-if="row.detail?.risk" size="small" effect="plain">{{ row.detail.risk }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="user" label="操作人" width="180" show-overflow-tooltip />
            <el-table-column prop="target" label="对象" width="100" />
            <el-table-column label="详情" min-width="260">
              <template #default="{ row }"><code>{{ row.action === 'tool_call' ? row.detail.summary : JSON.stringify(row.detail) }}</code></template>
            </el-table-column>
            <el-table-column prop="result" label="结果" width="110" />
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>
    <CaseFormDialog v-model="caseOpen" :event-id="caseCmd?.command_id"
                    :title="caseCmd ? `${caseCmd.station_name} ${caseCmd.param_text}调整` : ''" :symptom="caseCmd?.description" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BreakerPanel from '@/components/BreakerPanel.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getData, postData } from '@/api/http'
import type { Command, Draft } from '@/api/types'
import { useAuth } from '@/stores/auth'
import CaseFormDialog from '@/components/kb/CaseFormDialog.vue'
import { useRealtime } from '@/stores/realtime'
import { fmt } from '@/utils/format'

const STATUS: Record<string, string> = {
  revoked: '令牌作废（熔断）', cancelled: '已取消（熔断）',
  drafted: '已草拟', checked: '已校验', blocked: '已拦截', pending_approval: '待审批', approved: '已批准',
  executed: '已执行', rejected: '已驳回', expired: '已过期',
}
const router = useRouter()
const rt = useRealtime()
const auth = useAuth()
const route = useRoute()
const tab = ref((route.query.tab as string) || 'cmd')
const tripped = computed(() => Object.values(rt.snap?.breakers ?? {}).filter((b) => b.state === 'tripped').length)
const cmds = ref<Command[]>([])
const drafts = ref<Draft[]>([])
const pending = ref<Draft[]>([])
const audit = ref<any[]>([])
const comments = ref<Record<string, string>>({})
const auditFilter = ref('')
const caseOpen = ref(false)
const caseCmd = ref<Command | null>(null)
const auditRows = computed(() =>
  audit.value.filter((a) => !auditFilter.value || (auditFilter.value === 'tool_call' ? a.action === 'tool_call' : a.action !== 'tool_call')),
)
let first = true

async function load() {
  const ctl = auth.hasAny('control:draft', 'control:approve')
  ;[cmds.value, drafts.value, pending.value, audit.value] = await Promise.all([
    getData<Command[]>('/api/control/commands'),
    ctl ? getData<Draft[]>('/api/control/drafts') : Promise.resolve([]),
    ctl ? getData<Draft[]>('/api/control/approvals') : Promise.resolve([]),
    auth.has('audit:read') ? getData<any[]>('/api/audit') : Promise.resolve([]),
  ])
  if (first && !route.query.tab && pending.value.length && auth.has('control:approve')) tab.value = 'approve'
  first = false
}
async function execute(d: Draft) {
  await ElMessageBox.confirm(`凭 ${d.approval?.approver} 签发的令牌下发：${d.station_name} ${d.description}？`, '下发', { type: 'warning' })
  const c = await postData<Command>(`/api/control/drafts/${d.draft_id}/execute`, { token: d.approval!.token })
  ElMessage.success(`指令 ${c.command_id} 已下发`)
  load()
}
async function decide(d: Draft, ok: boolean) {
  if (!ok) await ElMessageBox.confirm(`驳回 ${d.station_name} 的调控请求？`, '驳回', { type: 'warning' })
  await postData(`/api/control/drafts/${d.draft_id}/${ok ? 'approve' : 'reject'}`, {
    comment: comments.value[d.draft_id] || (ok ? '同意下发' : '驳回'),
  })
  ElMessage.success(ok ? '已签发令牌，等待智能体下发' : '已驳回')
  load()
  rt.poll()
}
async function rollback(c: Command) {
  const { value } = await ElMessageBox.prompt(`回滚 ${c.station_name} 的指令 ${c.command_id}，请输入原因`, '回滚', { inputValue: '人工回滚' })
  await postData(`/api/control/commands/${c.command_id}/rollback`, { reason: value })
  ElMessage.success('已回滚')
  load()
}
let t: number
onMounted(() => {
  load()
  t = window.setInterval(load, 5000)
})
onUnmounted(() => clearInterval(t))
</script>

<style scoped>
code { font-size: 12px; color: #9fb4d8; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; margin-bottom: 12px; }
.small { font-size: 12px; }
.mb { margin-bottom: 10px; }
.tab-badge { margin-left: 4px; }
.req { margin-bottom: 12px; border-left: 3px solid var(--warn); }
.req-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.ml-auto { margin-left: auto; font-size: 12px; }
.desc { margin: 8px 0 4px; }
.reason { font-size: 13px; margin-bottom: 8px; color: #c7d6ee; }
.two { grid-template-columns: 1fr 1fr; }
.pred { width: 100%; border-collapse: collapse; font-size: 13px; }
.pred th, .pred td { border: 1px solid var(--border); padding: 5px 8px; text-align: center; }
.pred th { background: #13203a; color: var(--muted); font-weight: 500; }
.checks { font-size: 12px; background: #0c1528; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.verdict { font-weight: 700; margin-bottom: 4px; }
.verdict.pass, .ck .pass { color: var(--ok); }
.verdict.warn, .ck .warn { color: var(--warn); }
.verdict.block, .ck .block { color: var(--danger); }
.ck { display: flex; gap: 6px; padding: 1px 0; }
.ck .nm { width: 80px; flex-shrink: 0; }
.ops { display: flex; gap: 8px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
@media (max-width: 1100px) { .two { grid-template-columns: 1fr; } }
</style>
