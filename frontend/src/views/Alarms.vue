<template>
  <div class="page" v-if="snap">
    <div class="grid kpis">
      <KpiCard label="活动报警" :value="snap.alarms.length" tone="danger" />
      <KpiCard label="一级" :value="count(1)" tone="danger" />
      <KpiCard label="二级" :value="count(2)" tone="warn" />
      <KpiCard label="三级" :value="count(3)" />
      <KpiCard label="未确认" :value="snap.alarms.filter((a) => !a.acked).length" />
    </div>
    <div class="panel">
      <div class="panel-title">活动报警</div>
      <el-table :data="snap.alarms" size="small" empty-text="当前无报警">
        <el-table-column label="级别" width="70"><template #default="{ row }"><el-tag size="small" :type="LEVEL_TYPE[row.level]" effect="dark">{{ LEVEL_TEXT[row.level] }}</el-tag></template></el-table-column>
        <el-table-column label="对象" width="150"><template #default="{ row }"><router-link :to="link(row.object_id)">{{ row.object_name }}</router-link></template></el-table-column>
        <el-table-column prop="message" label="报警内容" min-width="300" />
        <el-table-column label="首次发生" width="160"><template #default="{ row }">{{ row.first_ts.replace('T', ' ') }}</template></el-table-column>
        <el-table-column label="持续" width="90"><template #default="{ row }">{{ dur(row.first_ts) }}</template></el-table-column>
        <el-table-column v-if="auth.has('kb:read')" label="知识库" width="96">
          <template #default="{ row }"><el-button size="small" link type="success" @click="advice(row.id)">处置建议</el-button></template>
        </el-table-column>
        <el-table-column label="确认" width="120">
          <template #default="{ row }">
            <span v-if="row.acked" class="muted">已确认</span>
            <el-button v-else-if="auth.has('alarm:ack')" size="small" link type="primary" @click="ack(row.id)">确认</el-button>
            <span v-else class="muted">未确认</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="panel">
      <div class="panel-title">报警历史 <el-button size="small" @click="loadHist">刷新</el-button></div>
      <el-table :data="hist" size="small" max-height="400">
        <el-table-column label="级别" width="70"><template #default="{ row }">{{ LEVEL_TEXT[row.level] }}</template></el-table-column>
        <el-table-column prop="object_name" label="对象" width="150" />
        <el-table-column prop="message" label="内容" min-width="300" />
        <el-table-column label="发生" width="160"><template #default="{ row }">{{ row.first_ts.replace('T', ' ') }}</template></el-table-column>
        <el-table-column label="恢复" width="160"><template #default="{ row }">{{ row.cleared_ts?.replace('T', ' ') ?? (row.active ? '未恢复' : '') }}</template></el-table-column>
        <el-table-column v-if="auth.has('kb:write')" label="" width="100">
          <template #default="{ row }"><el-button size="small" link @click="saveCase(row)">沉淀为案例</el-button></template>
        </el-table-column>
      </el-table>
    </div>
    <KbAdviceDrawer v-model="adviceOpen" :alarm-id="adviceId" />
    <CaseFormDialog v-model="caseOpen" :event-id="caseEvent?.id" :title="caseEvent ? `${caseEvent.object_name}：${caseEvent.message}` : ''"
                    :symptom="caseEvent?.message" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import KpiCard from '@/components/KpiCard.vue'
import CaseFormDialog from '@/components/kb/CaseFormDialog.vue'
import KbAdviceDrawer from '@/components/kb/KbAdviceDrawer.vue'
import { getData, postData } from '@/api/http'
import type { Alarm } from '@/api/types'
import { useAuth } from '@/stores/auth'
import { useRealtime } from '@/stores/realtime'
import { LEVEL_TEXT, LEVEL_TYPE } from '@/utils/format'

const rt = useRealtime()
const auth = useAuth()
const { snap } = storeToRefs(rt)
const hist = ref<Alarm[]>([])
const adviceOpen = ref(false)
const adviceId = ref<string | null>(null)
const caseOpen = ref(false)
const caseEvent = ref<Alarm | null>(null)
function advice(id: string) {
  adviceId.value = id
  adviceOpen.value = true
}
function saveCase(a: Alarm) {
  caseEvent.value = a
  caseOpen.value = true
}
const count = (l: number) => snap.value!.alarms.filter((a) => a.level === l).length
const link = (id: string) => (id.startsWith('HS') ? `/sources/${id}` : `/stations/${id}`)
const dur = (ts: string) => {
  const m = Math.round((new Date(snap.value!.ts).getTime() - new Date(ts).getTime()) / 60000)
  return m < 60 ? `${m} 分钟` : `${(m / 60).toFixed(1)} 小时`
}
async function ack(id: string) {
  await postData(`/api/alarms/${id}/ack`)
  ElMessage.success('已确认')
  rt.poll()
}
async function loadHist() {
  hist.value = await getData<Alarm[]>('/api/alarms?history=true')
}
onMounted(loadHist)
</script>

<style scoped>
.kpis { grid-template-columns: repeat(5, minmax(0, 1fr)); }
</style>
