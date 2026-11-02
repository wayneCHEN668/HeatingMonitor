<template>
  <el-drawer v-model="visible" :title="title" size="620px" destroy-on-close>
    <div v-loading="loading">
      <div v-if="summary" class="summary">{{ summary }}</div>
      <div class="sec-title">相似历史案例 <span class="muted">（仅已审核通过的案例）</span></div>
      <el-empty v-if="!loading && !cases.length" description="没有找到相似案例" :image-size="50" />
      <CaseCard v-for="c in cases" :key="c.case_id" :c="c" />
      <template v-if="docs.length">
        <div class="sec-title">相关规程与手册</div>
        <div v-for="d in docs" :key="d.chunk_id" class="doc" @click="openDoc(d)">
          <div class="dh"><el-tag size="small">{{ d.type }}</el-tag><b>{{ d.title }}</b><span class="muted">{{ d.section }}</span></div>
          <div class="snip"><HlText :text="d.snippet" :terms="d.highlights" /></div>
        </div>
      </template>
      <div class="foot muted">知识库内容为参考资料，处置以现场核实为准。</div>
      <el-button v-if="alarmId && auth.has('kb:write')" type="primary" plain @click="form = true">处置完成后沉淀为案例</el-button>
    </div>
    <DocViewer v-model="docOpen" :doc-id="docId" :section="docSection" :terms="docTerms" />
    <CaseFormDialog v-model="form" :event-id="alarmId" :title="eventTitle" :symptom="summaryEvent" />
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '@/api/http'
import type { KbCase, KbResult } from '@/api/types'
import { useAuth } from '@/stores/auth'
import CaseCard from './CaseCard.vue'
import CaseFormDialog from './CaseFormDialog.vue'
import DocViewer from './DocViewer.vue'
import HlText from './HlText.vue'

/** 两种用法：传 alarmId 查报警处置建议；或传 query 按症状/特征查相似案例 */
const visible = defineModel<boolean>({ default: false })
const props = defineProps<{
  alarmId?: string | null
  query?: { symptom: string; features?: string[]; alarm_codes?: string[]; object_type?: string; label?: string } | null
}>()
const auth = useAuth()
const loading = ref(false)
const cases = ref<KbCase[]>([])
const docs = ref<KbResult[]>([])
const summary = ref('')
const event = ref<any>(null)
const form = ref(false)
const docOpen = ref(false)
const docId = ref<string | null>(null)
const docSection = ref('')
const docTerms = ref<string[]>([])
const title = computed(() => (props.alarmId ? '报警处置建议' : `相似案例 · ${props.query?.label ?? ''}`))
const eventTitle = computed(() => (event.value ? `${event.value.object_name}：${event.value.description}` : ''))
const summaryEvent = computed(() => event.value?.description ?? '')

watch(visible, async (v) => {
  if (!v) return
  loading.value = true
  cases.value = []
  docs.value = []
  try {
    if (props.alarmId) {
      const r = await api.get<any>(`/api/kb/advice?alarm_id=${encodeURIComponent(props.alarmId)}`)
      cases.value = r.data.cases
      docs.value = r.data.docs
      event.value = r.data.event
      summary.value = r.summary
    } else if (props.query) {
      const r = await api.post<any>('/api/kb/cases/similar', { ...props.query, top_k: 3 })
      cases.value = r.data.cases
      summary.value = r.summary
      if (props.query.symptom) {
        const s = await api.get<any>(`/api/kb/search?q=${encodeURIComponent(props.query.symptom)}&top_k=3`)
        docs.value = s.data.results
      }
    }
  } finally {
    loading.value = false
  }
})

function openDoc(d: KbResult) {
  docId.value = d.doc_id
  docSection.value = d.section
  docTerms.value = d.highlights
  docOpen.value = true
}
</script>

<style scoped>
.summary { background: #102544; border: 1px solid #24406d; border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; font-size: 13px; }
.sec-title { font-weight: 600; margin: 12px 0 8px; }
.doc { border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
.doc:hover { border-color: var(--accent); }
.dh { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.snip { font-size: 13px; color: #c7d6ee; margin-top: 4px; line-height: 1.7; }
.foot { font-size: 12px; margin: 12px 0; }
</style>
