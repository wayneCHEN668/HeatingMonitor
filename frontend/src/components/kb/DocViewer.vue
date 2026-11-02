<template>
  <el-drawer v-model="visible" :title="doc ? `${doc.doc_id} ${doc.title}` : '文档'" size="560px" destroy-on-close>
    <div v-loading="loading" class="doc">
      <template v-if="doc">
        <div class="meta">
          <el-tag size="small">{{ doc.type }}</el-tag>
          <span class="muted">版本 {{ doc.version || '—' }} · 更新 {{ doc.updated || '—' }} · {{ doc.owner || doc.uploaded_by || '—' }}</span>
          <el-tag v-if="doc.sample" size="small" type="warning" effect="plain">示例文档</el-tag>
        </div>
        <el-alert v-if="doc.flags.length" type="warning" show-icon :closable="false" class="mb"
                  :title="`文档含疑似指令性文字（${doc.flags.join('、')}），内容仅供参考`" />
        <template v-for="(b, i) in blocks" :key="i">
          <h3 v-if="b.kind === 'h2'" :class="{ focus: isFocus(b.text) }" :ref="(el) => isFocus(b.text) && (focusEl = el as HTMLElement)">{{ b.text }}</h3>
          <h4 v-else-if="b.kind === 'h3'" :class="{ focus: isFocus(b.text) }" :ref="(el) => isFocus(b.text) && (focusEl = el as HTMLElement)">{{ b.text }}</h4>
          <div v-else-if="b.kind === 'li'" class="li">• <HlText :text="b.text" :terms="terms" /></div>
          <div v-else-if="b.kind === 'oli'" class="li"><HlText :text="b.text" :terms="terms" /></div>
          <p v-else-if="b.kind === 'p'"><HlText :text="b.text" :terms="terms" /></p>
        </template>
      </template>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { getData } from '@/api/http'
import type { KbDoc } from '@/api/types'
import { mdBlocks } from '@/utils/text'
import HlText from './HlText.vue'

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ docId: string | null; section?: string; terms?: string[] }>()
const doc = ref<KbDoc | null>(null)
const loading = ref(false)
const focusEl = ref<HTMLElement | null>(null)
const blocks = computed(() => mdBlocks(doc.value?.body ?? '').filter((b) => b.kind !== 'h1'))
const focusParts = computed(() => (props.section ?? '').split(' › ').filter(Boolean))
const isFocus = (t: string) => focusParts.value.includes(t)

watch(visible, async (v) => {
  if (!v || !props.docId) return
  loading.value = true
  focusEl.value = null
  try {
    doc.value = await getData<KbDoc>(`/api/kb/docs/${props.docId}`)
  } finally {
    loading.value = false
  }
  await nextTick()
  ;(focusEl.value as HTMLElement | null)?.scrollIntoView({ block: 'start' })   // 由模板 ref 回调在渲染时赋值
})
</script>

<style scoped>
.doc { line-height: 1.85; color: #d3deef; }
.meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; font-size: 12px; }
.mb { margin-bottom: 10px; }
h3 { font-size: 15px; margin: 18px 0 6px; color: #fff; }
h4 { font-size: 14px; margin: 12px 0 4px; color: #e8f0fc; }
.focus { color: #ffd58a; border-left: 3px solid #f5a524; padding-left: 8px; }
p { margin: 4px 0 8px; }
.li { padding-left: 12px; margin: 2px 0; }
</style>
