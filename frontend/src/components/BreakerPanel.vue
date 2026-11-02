<template>
  <div class="breakers" v-loading="!rows">
    <div class="panel intro">
      同一管网 {{ limits?.window_min ?? 60 }} 分钟内回滚达到 {{ limits?.rollbacks ?? 3 }} 次，或数据缺失率超过
      {{ pct(limits?.missing_rate) }}（至少累积 {{ limits?.min_coverage_min ?? 10 }} 分钟数据），自动熔断：
      该管网自治等级降为 <b>L1</b>——不再自动签发令牌，已签发未执行的令牌作废，智能体只能草拟和校验、不能提交审批或下发；
      人工操作照常，现场设备保持当前设定。熔断不会自动解除，须由值班调度长复核后解除。
    </div>
    <div class="grid cards">
      <div v-for="r in rows ?? []" :key="r.network_id" class="panel card" :class="r.state">
        <div class="head">
          <b>{{ r.name }}</b>
          <el-tag :type="r.state === 'tripped' ? 'danger' : 'success'" effect="dark" size="small">
            {{ r.state === 'tripped' ? (r.manual ? '人工熔断' : '已熔断') : '正常' }}</el-tag>
          <span class="muted small ml-auto">累计熔断 {{ r.trip_count }} 次</span>
        </div>
        <el-alert v-if="r.state === 'tripped'" type="error" :closable="false" show-icon class="mb"
                  :title="`${r.tripped_at?.replace('T', ' ')} 起降为 L1`" :description="r.reasons.join('；')" />
        <div class="metric">
          <span class="k">回滚次数（{{ r.limits.window_min }} 分钟）</span>
          <el-progress :percentage="Math.min(100, (r.metrics.rollbacks / r.limits.rollbacks) * 100)" :stroke-width="8"
                       :color="r.metrics.rollbacks >= r.limits.rollbacks ? '#ef4444' : r.metrics.rollbacks ? '#f5a524' : '#22c55e'"
                       :format="() => `${r.metrics.rollbacks} / ${r.limits.rollbacks}`" />
        </div>
        <div class="metric">
          <span class="k">数据缺失率</span>
          <el-progress :percentage="Math.min(100, ((r.metrics.missing_rate ?? 0) / r.limits.missing_rate) * 100)" :stroke-width="8"
                       :color="(r.metrics.missing_rate ?? 0) > r.limits.missing_rate ? '#ef4444' : '#22c55e'"
                       :format="() => `${pct(r.metrics.missing_rate)} / ${pct(r.limits.missing_rate)}`" />
        </div>
        <div class="muted small">已累积 {{ Math.round(r.metrics.coverage_s / 60) }} 分钟数据 · 最近 {{ r.limits.reset_window_min }} 分钟缺失率
          {{ pct(r.metrics.recent_missing_rate) }}</div>
        <div class="stations">
          <span v-for="s in r.stations" :key="s.id" class="st">{{ s.name }}
            <el-tag size="small" :type="s.effective !== s.configured ? 'danger' : 'info'" effect="plain">
              {{ s.effective !== s.configured ? `${s.configured}→${s.effective}` : s.effective }}</el-tag></span>
        </div>
        <div v-if="auth.has('control:approve')" class="ops">
          <el-input v-model="notes[r.network_id]" size="small"
                    :placeholder="r.state === 'tripped' ? '复核结论（必填），如：已查明原因并处理' : '人工熔断原因，如：管网检修'" />
          <el-button v-if="r.state === 'tripped'" size="small" type="primary" @click="act(r, 'reset')">复核后解除</el-button>
          <el-button v-else size="small" type="danger" plain @click="act(r, 'trip')">人工熔断</el-button>
        </div>
        <el-collapse v-if="r.events.length" class="events">
          <el-collapse-item :title="`熔断记录（${r.events.length}）`">
            <div v-for="(e, i) in r.events" :key="i" class="ev small">
              <span class="muted">{{ e.ts.replace('T', ' ') }}</span>
              <el-tag size="small" :type="e.action === 'trip' ? 'danger' : 'success'">{{ e.action === 'trip' ? '熔断' : '解除' }}</el-tag>
              {{ e.by }}：{{ e.action === 'trip' ? e.reasons.join('；') : e.comment }}
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getData } from '@/api/http'
import { useAuth } from '@/stores/auth'
import { useRealtime } from '@/stores/realtime'

const auth = useAuth()
const rt = useRealtime()
const rows = ref<any[] | null>(null)
const notes = ref<Record<string, string>>({})
const limits = computed(() => rows.value?.[0]?.limits)
const pct = (v?: number | null) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`)

async function load() {
  rows.value = await getData<any[]>('/api/breaker')
}
async function act(r: any, kind: 'reset' | 'trip') {
  const comment = (notes.value[r.network_id] ?? '').trim()
  if (kind === 'trip') {
    await ElMessageBox.confirm(`人工熔断 ${r.name}？该管网所有站将降为 L1，已签发未执行的令牌作废。`, '人工熔断', { type: 'warning' })
  }
  try {
    const res = await api.post(`/api/breaker/${r.network_id}/${kind}`, { comment })
    ElMessage.success(res.summary)
    notes.value[r.network_id] = ''
    load()
    rt.poll()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
let t: number
onMounted(() => {
  load()
  t = window.setInterval(load, 5000)
})
onUnmounted(() => clearInterval(t))
defineExpose({ load })
</script>

<style scoped>
.intro { font-size: 13px; line-height: 1.8; color: #c7d6ee; margin-bottom: 12px; }
.cards { grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }
.card { border-left: 3px solid var(--ok); }
.card.tripped { border-left-color: var(--danger); background: linear-gradient(160deg, #2a1215, #0f1a2e 60%); }
.head { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.ml-auto { margin-left: auto; }
.mb { margin-bottom: 10px; }
.metric { display: grid; grid-template-columns: 150px 1fr; align-items: center; gap: 8px; margin: 6px 0; font-size: 13px; }
.metric .k { color: var(--muted); }
.small { font-size: 12px; }
.stations { display: flex; flex-wrap: wrap; gap: 6px 12px; margin: 10px 0; font-size: 12px; }
.st { display: inline-flex; gap: 4px; align-items: center; }
.ops { display: flex; gap: 8px; margin-top: 8px; }
.events { margin-top: 8px; --el-collapse-header-bg-color: transparent; --el-collapse-content-bg-color: transparent; }
.ev { display: flex; gap: 6px; align-items: center; padding: 2px 0; flex-wrap: wrap; }
</style>
