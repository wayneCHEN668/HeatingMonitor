<template>
  <div class="page">
    <div class="row">
      <el-button size="small" @click="router.push('/workbench')">← 工作台</el-button>
      <template v-if="v">
        <el-button size="small" :type="v.pinned ? 'warning' : 'primary'" @click="pin(!v.pinned)">
          {{ v.pinned ? '从工作台移除' : '固定到工作台' }}</el-button>
        <el-button size="small" @click="load">刷新数据</el-button>
      </template>
    </div>
    <div class="panel" v-loading="!v && !err">
      <el-result v-if="err" icon="warning" :title="err" />
      <template v-if="v">
        <div class="panel-title">
          <span>{{ v.title }} <span class="sub">{{ v.description }}</span></span>
          <span class="sub">{{ v.created_by }}{{ v.actor_kind === 'agent' ? '（智能体生成）' : '' }} · {{ v.created_at.replace('T', ' ') }}</span>
        </div>
        <VizView :v="v" height="420px" />
        <div class="muted small foot">
          数据由平台按您的权限和数据范围实时获取<span v-if="v.rendered_at">（{{ v.rendered_at.replace('T', ' ') }}）</span>，生成方（含智能体）只提供图表规格，不经手数据。
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import VizView from '@/components/VizView.vue'
import { api, getData } from '@/api/http'

const route = useRoute()
const router = useRouter()
const v = ref<any>(null)
const err = ref('')
async function load() {
  try {
    v.value = await getData(`/api/viz/${route.params.id}`)
    err.value = ''
  } catch (e: any) {
    err.value = e.message
  }
}
async function pin(pinned: boolean) {
  try {
    const r = await api.post<any>(`/api/viz/${route.params.id}/pin`, { pinned })
    v.value.pinned = r.data.pinned
    ElMessage.success(r.summary)
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
let t: number
onMounted(() => {
  load()
  t = window.setInterval(() => v.value?.mode === 'chart' && load(), 60_000)
})
onUnmounted(() => clearInterval(t))
watch(() => route.params.id, (id) => id && load())
</script>

<style scoped>
.small { font-size: 12px; }
.foot { margin-top: 8px; }
</style>
