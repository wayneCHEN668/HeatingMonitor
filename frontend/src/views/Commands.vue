<template>
  <div class="page">
    <el-tabs v-model="tab">
      <el-tab-pane label="指令记录" name="cmd">
        <div class="panel">
          <el-table :data="cmds" size="small" empty-text="暂无下发记录（可在热力站详情页或 AI 曲线优化中下发）">
            <el-table-column prop="command_id" label="指令号" width="120" />
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.executed_at.replace('T', ' ') }}</template></el-table-column>
            <el-table-column prop="station_name" label="站点" width="110" />
            <el-table-column prop="param_text" label="参数" width="110" />
            <el-table-column prop="description" label="内容" min-width="240" show-overflow-tooltip />
            <el-table-column label="来源" width="70"><template #default="{ row }"><el-tag size="small" :type="row.source === 'ai' ? 'success' : 'info'">{{ row.source === 'ai' ? 'AI' : '人工' }}</el-tag></template></el-table-column>
            <el-table-column prop="approver" label="审批" width="130" />
            <el-table-column label="回执" width="80"><template #default="{ row }">{{ row.ack.latency_ms }}ms</template></el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }"><el-tag size="small" :type="row.status === 'executed' ? 'success' : 'info'">{{ row.status === 'executed' ? '已执行' : '已回滚' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }"><el-button v-if="row.status === 'executed'" size="small" link type="danger" @click="rollback(row)">回滚</el-button></template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane label="草案" name="draft">
        <div class="panel">
          <el-table :data="drafts" size="small">
            <el-table-column prop="draft_id" label="草案号" width="110" />
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.created_at.replace('T', ' ') }}</template></el-table-column>
            <el-table-column prop="station_name" label="站点" width="110" />
            <el-table-column prop="description" label="内容" min-width="240" show-overflow-tooltip />
            <el-table-column label="校验" width="90"><template #default="{ row }">
              <el-tag v-if="row.check" size="small" :type="({ pass: 'success', warn: 'warning', block: 'danger' } as any)[row.check.verdict]">{{ row.check.verdict }}</el-tag>
            </template></el-table-column>
            <el-table-column prop="status" label="状态" width="90" />
            <el-table-column label="审批人" width="130"><template #default="{ row }">{{ row.approval?.approver }}</template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane label="审计日志" name="audit">
        <div class="panel">
          <el-table :data="audit" size="small">
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.ts.replace('T', ' ') }}</template></el-table-column>
            <el-table-column prop="action" label="动作" width="120" />
            <el-table-column prop="user" label="操作人" width="140" />
            <el-table-column prop="target" label="对象" width="80" />
            <el-table-column label="详情" min-width="260"><template #default="{ row }"><code>{{ JSON.stringify(row.detail) }}</code></template></el-table-column>
            <el-table-column prop="result" label="结果" width="160" />
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getData, postData } from '@/api/http'
import type { Command, Draft } from '@/api/types'

const tab = ref('cmd')
const cmds = ref<Command[]>([])
const drafts = ref<Draft[]>([])
const audit = ref<any[]>([])
async function load() {
  ;[cmds.value, drafts.value, audit.value] = await Promise.all([
    getData<Command[]>('/api/control/commands'),
    getData<Draft[]>('/api/control/drafts'),
    getData<any[]>('/api/audit'),
  ])
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
  t = window.setInterval(load, 10000)
})
onUnmounted(() => clearInterval(t))
</script>

<style scoped>
code { font-size: 12px; color: #9fb4d8; }
</style>
