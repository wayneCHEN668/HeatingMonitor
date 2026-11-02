<template>
  <el-dialog v-model="visible" :title="`调控指令下发 · ${draft?.station_name ?? ''}`" width="760px" :close-on-click-modal="false"
             destroy-on-close @closed="cleanup">
    <el-steps :active="step" finish-status="success" align-center class="steps">
      <el-step title="草拟" />
      <el-step title="安全校验" />
      <el-step title="审批签发" />
      <el-step title="下发执行" />
      <el-step title="效果跟踪" />
    </el-steps>

    <div v-loading="busy" class="body">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb" />

      <!-- 草案 -->
      <template v-if="draft">
        <div class="desc">
          <el-tag size="small" :type="draft.source === 'ai' ? 'success' : 'info'">{{ draft.source === 'ai' ? 'AI 建议' : '人工' }}</el-tag>
          <b>{{ draft.param_text }}</b>：{{ draft.description }}
          <span class="muted">（{{ draft.draft_id }}）</span>
        </div>
        <table class="pred">
          <thead>
            <tr><th>稳态仿真预测</th><th>二次供温</th><th>阀门开度</th><th>热功率</th><th>典型室温</th><th>一次回温</th></tr>
          </thead>
          <tbody>
            <tr>
              <td class="muted">调整前</td>
              <td class="num">{{ fmt(draft.expected.before.ts2) }}℃</td>
              <td class="num">{{ fmt(draft.expected.before.valve) }}%</td>
              <td class="num">{{ fmt(draft.expected.before.heat_mw, 2) }}MW</td>
              <td class="num">{{ fmt(draft.expected.before.tin) }}℃</td>
              <td class="num">{{ fmt(draft.expected.before.tr1) }}℃</td>
            </tr>
            <tr>
              <td>调整后</td>
              <td class="num">{{ fmt(draft.expected.after.ts2) }}℃</td>
              <td class="num">{{ fmt(draft.expected.after.valve) }}%</td>
              <td class="num">{{ fmt(draft.expected.after.heat_mw, 2) }}MW <small :class="dHeat <= 0 ? 'ok' : 'warn'">{{ pct(dHeat, 1, true) }}</small></td>
              <td class="num" :class="tinCls(draft.expected.after.tin)">{{ fmt(draft.expected.after.tin) }}℃</td>
              <td class="num">{{ fmt(draft.expected.after.tr1) }}℃</td>
            </tr>
          </tbody>
        </table>
      </template>

      <!-- 校验 -->
      <div v-if="draft?.check" class="check">
        <div class="verdict" :class="draft.check.verdict">
          校验结论：{{ { pass: '通过', warn: '通过（有告警）', block: '拦截' }[draft.check.verdict] }}
        </div>
        <div v-for="it in draft.check.items" :key="it.name" class="item">
          <el-icon :class="it.status"><component :is="icon(it.status)" /></el-icon>
          <span class="nm">{{ it.name }}</span>
          <span class="msg">{{ it.message }}</span>
        </div>
      </div>

      <!-- 审批 -->
      <div v-if="step === 2 && draft?.status === 'checked'" class="approve">
        <el-form inline size="small">
          <el-form-item label="审批人"><el-select v-model="approver" style="width: 140px">
            <el-option v-for="a in ['值班调度长', '运行主任', '技术总工']" :key="a" :value="a" />
          </el-select></el-form-item>
          <el-form-item label="意见"><el-input v-model="comment" placeholder="同意下发" style="width: 260px" /></el-form-item>
        </el-form>
      </div>
      <el-alert v-if="draft?.approval" type="success" :closable="false" show-icon class="mb"
                :title="`${draft.approval.approver} 已签发令牌，有效期至 ${draft.approval.expires_at.slice(11)}`"
                :description="draft.approval.comment" />

      <!-- 执行结果 / 跟踪 -->
      <div v-if="cmd" class="track">
        <el-alert type="success" :closable="false" show-icon class="mb"
                  :title="`指令 ${cmd.command_id} 已下发，设备回执 ${cmd.ack.latency_ms}ms`" />
        <div v-if="effect" class="grid track-grid">
          <div class="tk">
            <div class="muted">阀门开度</div>
            <div class="num big">{{ fmt(effect.tracking.valve.now) }}% <span class="muted">→ {{ fmt(effect.tracking.valve.target) }}%</span></div>
            <el-progress :percentage="progress(effect.tracking.valve.now, cmd.expected.valve, startValve)" :show-text="false" />
          </div>
          <div class="tk">
            <div class="muted">二次供温</div>
            <div class="num big">{{ fmt(effect.tracking.ts2.now) }}℃ <span class="muted">→ {{ fmt(effect.tracking.ts2.target) }}℃</span></div>
          </div>
          <div class="tk">
            <div class="muted">热功率</div>
            <div class="num big">{{ fmt(effect.tracking.heat_mw.now, 2) }} <span class="muted">→ {{ fmt(effect.tracking.heat_mw.expected, 2) }} MW</span></div>
          </div>
          <div class="tk">
            <div class="muted">典型室温（惯性大，缓慢变化）</div>
            <div class="num big">{{ fmt(effect.tracking.tin.now, 2) }}℃ <span class="muted">→ 稳态 {{ fmt(effect.tracking.tin.expected) }}℃</span></div>
          </div>
        </div>
        <div class="muted small" v-if="effect">
          已执行 {{ effect.tracking.elapsed_s }}s ·
          <el-tag size="small" :type="effect.settled ? 'success' : 'warning'">{{ effect.settled ? '已到位' : '调节中' }}</el-tag>
          <el-tag v-if="cmd.status === 'rolled_back'" size="small" type="info">已回滚</el-tag>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="visible = false">{{ cmd ? '完成' : '取消' }}</el-button>
      <template v-if="draft && !cmd">
        <el-button v-if="step === 0" type="primary" @click="doCheck">安全校验</el-button>
        <template v-if="step === 2 && draft.status === 'checked'">
          <el-button type="danger" plain @click="doReject">驳回</el-button>
          <el-button type="primary" @click="doApprove">确认并签发令牌</el-button>
        </template>
        <el-button v-if="draft.status === 'approved'" type="warning" @click="doExecute">下发到现场</el-button>
      </template>
      <el-button v-if="cmd && cmd.status === 'executed'" type="danger" plain @click="doRollback">回滚</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheckFilled, CircleCloseFilled, WarningFilled } from '@element-plus/icons-vue'
import { api } from '@/api/http'
import type { Command, CommandRequest, Draft } from '@/api/types'
import { fmt, pct } from '@/utils/format'

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ request: CommandRequest | null }>()
const emit = defineEmits<{ (e: 'done', c: Command): void }>()

const draft = ref<Draft | null>(null)
const cmd = ref<Command | null>(null)
const effect = ref<any>(null)
const busy = ref(false)
const error = ref('')
const approver = ref('值班调度长')
const comment = ref('')
const startValve = ref(0)
let timer: number | undefined

const step = computed(() => {
  if (cmd.value) return 4
  const s = draft.value?.status
  if (s === 'approved') return 3
  if (s === 'checked') return 2
  if (s === 'blocked' || s === 'rejected') return 1
  return draft.value ? 0 : 0
})
const dHeat = computed(() => {
  const d = draft.value
  if (!d) return 0
  return ((d.expected.after.heat_mw - d.expected.before.heat_mw) / Math.max(d.expected.before.heat_mw, 0.01)) * 100
})
const tinCls = (t: number) => (t < 18 ? 'danger' : t > 24 ? 'warn' : 'ok')
const icon = (s: string) => (s === 'pass' ? CircleCheckFilled : s === 'warn' ? WarningFilled : CircleCloseFilled)
const progress = (now: number, target: number, start: number) => {
  const span = Math.abs(target - start)
  if (span < 0.1) return 100
  return Math.max(0, Math.min(100, (1 - Math.abs(target - now) / span) * 100))
}

async function run<T>(fn: () => Promise<T>) {
  busy.value = true
  error.value = ''
  try {
    return await fn()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

watch(visible, async (v) => {
  if (!v || !props.request) return
  draft.value = null
  cmd.value = null
  effect.value = null
  await run(async () => {
    const r = await api.post<Draft>('/api/control/drafts', { ...props.request, user: '调度员' })
    draft.value = r.data
  })
})

const doCheck = () =>
  run(async () => {
    const r = await api.post<Draft>(`/api/control/drafts/${draft.value!.draft_id}/check`)
    draft.value = r.data
    if (r.data.status === 'approved') ElMessage.success('L3 站点：安全边界内由安全网关自动签发令牌')
  })
const doApprove = () =>
  run(async () => {
    draft.value = (await api.post<Draft>(`/api/control/drafts/${draft.value!.draft_id}/approve`,
      { approver: approver.value, comment: comment.value })).data
  })
const doReject = () =>
  run(async () => {
    draft.value = (await api.post<Draft>(`/api/control/drafts/${draft.value!.draft_id}/reject`,
      { approver: approver.value, comment: comment.value || '驳回' })).data
    ElMessage.info('草案已驳回')
  })
const doExecute = () =>
  run(async () => {
    const r = await api.post<Command>(`/api/control/drafts/${draft.value!.draft_id}/execute`, {
      token: draft.value!.approval!.token,
      user: '调度员',
    })
    cmd.value = r.data
    startValve.value = draft.value!.expected.before.valve
    ElMessage.success(r.summary)
    emit('done', r.data)
    poll()
    timer = window.setInterval(poll, 2000)
  })
async function poll() {
  if (!cmd.value) return
  try {
    effect.value = (await api.get<any>(`/api/control/commands/${cmd.value.command_id}/effect`)).data
    cmd.value = effect.value.command
  } catch {
    /* ignore */
  }
}
async function doRollback() {
  const { value } = await ElMessageBox.prompt('请输入回滚原因', '回滚指令', { inputValue: '效果不符合预期' })
  await run(async () => {
    cmd.value = (await api.post<Command>(`/api/control/commands/${cmd.value!.command_id}/rollback`, { reason: value })).data
    ElMessage.success('已回滚到下发前设定')
    emit('done', cmd.value!)
  })
}
function cleanup() {
  if (timer) clearInterval(timer)
  timer = undefined
}
onBeforeUnmount(cleanup)
</script>

<style scoped>
.steps { margin-bottom: 16px; }
.body { min-height: 160px; }
.mb { margin-bottom: 10px; }
.desc { margin-bottom: 10px; line-height: 1.7; }
.pred { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 12px; }
.pred th, .pred td { border: 1px solid var(--border); padding: 6px 8px; text-align: center; }
.pred th { background: #13203a; color: var(--muted); font-weight: 500; }
.check { background: #0c1528; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-bottom: 12px; }
.verdict { font-weight: 700; margin-bottom: 6px; }
.verdict.pass { color: var(--ok); } .verdict.warn { color: var(--warn); } .verdict.block { color: var(--danger); }
.item { display: flex; gap: 8px; align-items: center; font-size: 13px; padding: 3px 0; }
.item .nm { width: 88px; color: var(--muted); flex-shrink: 0; }
.item .pass { color: var(--ok); } .item .warn { color: var(--warn); } .item .block { color: var(--danger); }
.track-grid { grid-template-columns: 1fr 1fr; margin-bottom: 8px; }
.tk { background: #0c1528; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.big { font-size: 18px; margin: 4px 0; }
.small { font-size: 12px; display: flex; gap: 8px; align-items: center; }
</style>
