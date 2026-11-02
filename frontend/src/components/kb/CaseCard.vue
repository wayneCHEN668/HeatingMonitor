<template>
  <div class="case" :class="{ pending: c.status !== 'approved' }">
    <div class="head">
      <b>{{ c.title }}</b>
      <el-tag v-if="c.score !== undefined" size="small" type="success" effect="plain">匹配度 {{ c.score.toFixed(2) }}</el-tag>
      <el-tag v-if="c.status === 'pending_review'" size="small" type="warning">待审核</el-tag>
      <el-tag v-else-if="c.status === 'rejected'" size="small" type="info">已驳回</el-tag>
      <span class="muted id">{{ c.case_id }}</span>
    </div>
    <div class="muted meta">{{ c.object_name || '—' }} · {{ c.occurred_at }} · 处置人 {{ c.handler }}
      <el-tag v-for="a in c.alarm_codes" :key="a" size="small" effect="plain" class="tag">{{ a }}</el-tag>
      <el-tag v-for="t in c.tags" :key="t" size="small" type="info" effect="plain" class="tag">{{ t }}</el-tag>
    </div>
    <div v-if="c.match_reasons?.length" class="reasons">匹配依据：{{ c.match_reasons.join('；') }}</div>
    <el-alert v-if="c.flags?.length" type="warning" :closable="false" show-icon class="flag"
              :title="`含疑似指令性文字：${c.flags.join('、')}`" />
    <div class="row2"><span class="k">现象</span><span>{{ c.symptom }}</span></div>
    <div v-if="c.cause" class="row2"><span class="k">原因</span><span>{{ c.cause }}</span></div>
    <div class="row2"><span class="k">处置</span><ol><li v-for="(s, i) in c.steps" :key="i">{{ s }}</li></ol></div>
    <div class="row2"><span class="k">结果</span><span class="ok">{{ c.outcome }}</span></div>
    <div v-if="c.reviewed_by" class="muted small">审核：{{ c.reviewed_by }}{{ c.review_comment ? `（${c.review_comment}）` : '' }}</div>
    <div v-else-if="c.submitted_by" class="muted small">提交：{{ c.submitted_by }} · {{ c.submitted_at?.replace('T', ' ') }}</div>
    <slot />
  </div>
</template>

<script setup lang="ts">
import type { KbCase } from '@/api/types'

defineProps<{ c: KbCase }>()
</script>

<style scoped>
.case { border: 1px solid var(--border); border-left: 3px solid var(--ok); border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; background: #0c1528; }
.case.pending { border-left-color: var(--warn); }
.head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.id { margin-left: auto; font-size: 12px; font-family: ui-monospace, monospace; }
.meta { font-size: 12px; margin: 4px 0 6px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.reasons { font-size: 12px; color: #9fd4a8; margin-bottom: 6px; }
.flag { margin-bottom: 6px; }
.row2 { display: grid; grid-template-columns: 40px 1fr; gap: 6px; font-size: 13px; margin: 3px 0; }
.row2 .k { color: var(--muted); }
ol { margin: 0; padding-left: 18px; }
.small { font-size: 12px; margin-top: 4px; }
</style>
