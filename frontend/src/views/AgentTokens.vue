<template>
  <div class="page">
    <div class="panel intro">
      <div class="panel-title">智能体授权 <span class="sub">为大模型智能体（MCP）签发委托令牌</span></div>
      <p>智能体以“<b>{{ auth.me?.name }}</b> 的代理”身份工作：可用权限 = 智能体档案白名单 ∩ 你的权限，数据范围与你相同
        （{{ auth.me?.scope.global ? '全网' : rt.meta?.scope?.name }}）。<b>任何智能体都不能批准调控指令</b>，批准只能由值班调度长在平台上人工完成。
        令牌只在签发时显示一次，可随时撤销，撤销后立即失效。</p>
    </div>

    <div class="grid main">
      <div class="panel">
        <div class="panel-title">签发新令牌</div>
        <el-form label-width="96px" size="default">
          <el-form-item label="智能体名称"><el-input v-model="form.agent" :placeholder="profiles[form.profile]?.name" maxlength="30" /></el-form-item>
          <el-form-item label="智能体档案">
            <el-radio-group v-model="form.profile">
              <el-radio-button v-for="(p, k) in profiles" :key="k" :value="k">{{ p.name }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="有效期">
            <el-select v-model="form.days" style="width: 140px">
              <el-option v-for="d in [1, 7, 30, 90]" :key="d" :value="d" :label="`${d} 天`" />
            </el-select>
          </el-form-item>
          <el-form-item label="用途备注"><el-input v-model="form.note" maxlength="100" placeholder="如：Claude Desktop 调度助手" /></el-form-item>
          <el-form-item label="生效权限">
            <div class="perms">
              <el-tag v-for="p in effective" :key="p" size="small" :type="p.startsWith('control') ? 'warning' : 'info'" effect="plain">
                {{ catalog?.permissions[p] ?? p }}</el-tag>
              <span v-if="!effective.length" class="muted">无（档案与你的权限没有交集）</span>
              <div v-if="dropped.length" class="muted small">档案中你本人没有的权限不会授予：{{ dropped.map((p) => catalog?.permissions[p] ?? p).join('、') }}</div>
            </div>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :disabled="!effective.length" :loading="busy" @click="create">签发令牌</el-button>
          </el-form-item>
        </el-form>
      </div>

      <div class="panel">
        <div class="panel-title">已签发的令牌 <span class="sub">只保存登记信息，不保存令牌明文</span></div>
        <el-table :data="rows" size="small" empty-text="尚未签发">
          <el-table-column prop="agent" label="智能体" min-width="110" />
          <el-table-column v-if="auth.me?.role === 'admin'" prop="owner_name" label="委托人" width="90" />
          <el-table-column prop="profile_name" label="档案" width="96" />
          <el-table-column label="控制权" width="72">
            <template #default="{ row }">
              <el-tag size="small" :type="row.permissions.includes('control:execute') ? 'warning' : 'info'">
                {{ row.permissions.includes('control:execute') ? '可下发' : '只读' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="到期" width="100"><template #default="{ row }">{{ row.expires_at.slice(0, 10) }}</template></el-table-column>
          <el-table-column label="最近使用" width="130"><template #default="{ row }">{{ row.last_used?.replace('T', ' ').slice(5, 16) ?? '—' }}</template></el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag v-if="row.revoked" size="small" type="info">已撤销</el-tag>
              <el-button v-else size="small" link type="danger" @click="revoke(row)">撤销</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <el-dialog v-model="show" title="委托令牌（只显示这一次）" width="720px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon class="mb"
                title="请立即复制保存。令牌等同于你的部分权限，不要提交到代码仓库或发到聊天群里。" />
      <div class="code-block">
        <code class="tok">{{ issued }}</code>
        <el-button size="small" @click="copy(issued)">复制令牌</el-button>
      </div>
      <div class="cfg-title">Claude Code</div>
      <div class="code-block"><pre>{{ cliCmd }}</pre><el-button size="small" @click="copy(cliCmd)">复制</el-button></div>
      <div class="cfg-title">Claude Desktop（claude_desktop_config.json）</div>
      <div class="code-block"><pre>{{ desktopCfg }}</pre><el-button size="small" @click="copy(desktopCfg)">复制</el-button></div>
      <p class="muted small">把 <code>/绝对路径/backend/run_mcp.py</code> 换成本机上的实际路径；后端地址默认 http://127.0.0.1:8000。</p>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getData } from '@/api/http'
import type { AgentTokenRecord } from '@/api/types'
import { useAuth } from '@/stores/auth'
import { useRealtime } from '@/stores/realtime'

interface Catalog {
  permissions: Record<string, string>
  agent_profiles: Record<string, { name: string; permissions: string[] }>
}

const auth = useAuth()
const rt = useRealtime()
const catalog = ref<Catalog | null>(null)
const rows = ref<AgentTokenRecord[]>([])
const form = reactive({ agent: '', profile: 'dispatch', days: 30, note: '' })
const busy = ref(false)
const show = ref(false)
const issued = ref('')

const profiles = computed(() => catalog.value?.agent_profiles ?? {})
const effective = computed(() =>
  (profiles.value[form.profile]?.permissions ?? []).filter((p) => auth.me?.permissions.includes(p)),
)
const dropped = computed(() =>
  (profiles.value[form.profile]?.permissions ?? []).filter((p) => !auth.me?.permissions.includes(p)),
)
const cliCmd = computed(
  () => `claude mcp add heatnet -e HEATNET_TOKEN=${issued.value} -- python /绝对路径/backend/run_mcp.py --api http://127.0.0.1:8000`,
)
const desktopCfg = computed(() =>
  JSON.stringify(
    { mcpServers: { heatnet: { command: 'python', args: ['/绝对路径/backend/run_mcp.py', '--api', 'http://127.0.0.1:8000'],
      env: { HEATNET_TOKEN: issued.value } } } },
    null,
    2,
  ),
)

async function load() {
  rows.value = await getData<AgentTokenRecord[]>('/api/auth/agent-tokens')
}
onMounted(async () => {
  catalog.value = await getData<Catalog>('/api/auth/catalog')
  load()
})

async function create() {
  busy.value = true
  try {
    const r = await api.post<{ token: string; record: AgentTokenRecord }>('/api/auth/agent-tokens', form)
    issued.value = r.data.token
    show.value = true
    form.note = ''
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    busy.value = false
  }
}
async function revoke(row: AgentTokenRecord) {
  await ElMessageBox.confirm(`撤销「${row.agent}」的委托令牌？使用该令牌的智能体将立即失去访问权限。`, '撤销令牌', { type: 'warning' })
  await api.del(`/api/auth/agent-tokens/${row.jti}`)
  ElMessage.success('已撤销')
  load()
}
async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中复制')
  }
}
</script>

<style scoped>
.intro p { margin: 0; line-height: 1.8; color: #c7d6ee; }
.main { grid-template-columns: 420px minmax(0, 1fr); align-items: start; }
.perms { display: flex; flex-wrap: wrap; gap: 6px; }
.small { font-size: 12px; width: 100%; }
.mb { margin-bottom: 12px; }
.code-block { display: flex; gap: 10px; align-items: flex-start; background: #0b1426; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; }
.code-block pre, .tok { flex: 1; margin: 0; font-family: ui-monospace, monospace; font-size: 12px; color: #9fb4d8; white-space: pre-wrap; word-break: break-all; }
.cfg-title { font-size: 13px; color: var(--muted); margin: 6px 0 4px; }
@media (max-width: 1100px) { .main { grid-template-columns: 1fr; } }
</style>
