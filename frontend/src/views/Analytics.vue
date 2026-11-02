<template>
  <div class="page">
    <el-tabs v-model="tab" class="tabs">
      <el-tab-pane label="日运行分析" name="daily"><DailyReport v-if="tab === 'daily'" /></el-tab-pane>
      <el-tab-pane label="节能分析" name="energy"><EnergySaving v-if="tab === 'energy'" /></el-tab-pane>
      <el-tab-pane label="多维统计" name="multi"><MultiDim v-if="tab === 'multi'" /></el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import DailyReport from './analytics/DailyReport.vue'
import EnergySaving from './analytics/EnergySaving.vue'
import MultiDim from './analytics/MultiDim.vue'

const route = useRoute()
const router = useRouter()
const tab = ref((route.query.tab as string) || 'daily')
watch(tab, (t) => router.replace({ query: { tab: t } }))
</script>

<style scoped>
.tabs :deep(.el-tabs__header) { margin-bottom: 10px; }
</style>
