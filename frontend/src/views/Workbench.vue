<template>
  <div class="page">
    <div class="panel intro">
      工作台存放您（以及代您工作的智能体通过 <code>viz_render</code>）生成的图表和定位视图。固定的视图每次打开都按您的权限取最新数据重绘，图表每分钟刷新一次。
    </div>
    <div v-if="pinned.length" class="grid cards">
      <div v-for="p in pinned" :key="p.viz_id" class="panel">
        <div class="panel-title">
          <router-link :to="p.link">{{ p.title }}</router-link>
          <span class="row">
            <span class="sub">{{ p.description }}</span>
            <el-button size="small" link @click="pin(p, false)">移除</el-button>
          </span>
        </div>
        <VizView v-if="data[p.viz_id]" :v="data[p.viz_id]" height="260px" />
        <el-result v-else-if="errs[p.viz_id]" icon="warning" :sub-title="errs[p.viz_id]" />
        <div v-else v-loading="true" style="height: 120px" />
      </div>
    </div>
    <el-empty v-else description="还没有固定的视图。打开一个可视化后点“固定到工作台”" :image-size="70" />
    <div class="panel">
      <div class="panel-title"><span>最近生成</span><el-button size="small" @click="load">刷新</el-button></div>
      <el-table :data="recent" size="small" empty-text="暂无">
        <el-table-column label="标题" min-width="200"><template #default="{ row }"><router-link :to="row.link">{{ row.title }}</router-link></template></el-table-column>
        <el-table-column prop="description" label="内容" min-width="280" show-overflow-tooltip />
        <el-table-column label="生成" width="230" show-overflow-tooltip>
          <template #default="{ row }">{{ row.created_at.slice(5, 16).replace('T', ' ') }} {{ row.created_by }}</template>
        </el-table-column>
        <el-table-column label="" width="140">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="pin(row, true)">固定</el-button>
            <el-button size="small" link type="danger" @click="del(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import VizView from '@/components/VizView.vue'
import { api, getData } from '@/api/http'

const rows = ref<any[]>([])
const data = ref<Record<string, any>>({})
const errs = ref<Record<string, string>>({})
const pinned = computed(() => rows.value.filter((r) => r.pinned))
const recent = computed(() => rows.value.filter((r) => !r.pinned))

async function load() {
  rows.value = await getData<any[]>('/api/viz')
  await Promise.all(pinned.value.map(async (p) => {
    try {
      data.value[p.viz_id] = await getData(`/api/viz/${p.viz_id}`)
    } catch (e: any) {
      errs.value[p.viz_id] = e.message
    }
  }))
}
async function pin(row: any, pinnedNow: boolean) {
  try {
    ElMessage.success((await api.post<any>(`/api/viz/${row.viz_id}/pin`, { pinned: pinnedNow })).summary)
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
async function del(row: any) {
  try {
    await api.del(`/api/viz/${row.viz_id}`)
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
let t: number
onMounted(() => {
  load()
  t = window.setInterval(load, 60_000)
})
onUnmounted(() => clearInterval(t))
</script>

<style scoped>
.intro { font-size: 13px; color: #c7d6ee; line-height: 1.8; }
.cards { grid-template-columns: repeat(auto-fill, minmax(520px, 1fr)); }
@media (max-width: 760px) { .cards { grid-template-columns: 1fr; } }
</style>
