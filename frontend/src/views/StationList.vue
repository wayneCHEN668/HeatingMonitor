<template>
  <div class="page" v-if="snap && meta">
    <div class="panel filters">
      <el-radio-group v-model="source" size="small">
        <el-radio-button value="">全部热源</el-radio-button>
        <el-radio-button v-for="s in meta.sources" :key="s.id" :value="s.id">{{ s.name.slice(0, 4) }}</el-radio-button>
      </el-radio-group>
      <el-select v-model="status" size="small" style="width: 130px">
        <el-option label="全部状态" value="" />
        <el-option label="有报警" value="alarm" />
        <el-option label="室温偏低" value="cold" />
        <el-option label="室温偏高" value="hot" />
        <el-option label="通讯中断" value="offline" />
      </el-select>
      <el-input v-model="kw" size="small" placeholder="站名 / 编号" clearable style="width: 160px" />
      <div class="counts muted">
        共 <b>{{ list.length }}</b> 站 · 在线 <b class="ok">{{ list.filter((s) => s.online).length }}</b>
        · 报警 <b class="danger">{{ list.filter((s) => alarmsOf(s.id).length).length }}</b>
        · 合计 <b class="supply num">{{ fmt(list.reduce((a, s) => a + s.heat_mw, 0), 2) }}</b> MW
      </div>
      <el-radio-group v-model="view" size="small" class="ml-auto">
        <el-radio-button value="card">卡片</el-radio-button>
        <el-radio-button value="table">表格</el-radio-button>
      </el-radio-group>
    </div>

    <div v-if="view === 'card'" class="grid cards">
      <div v-for="s in list" :key="s.id" class="panel card" :class="cardCls(s)" @click="open(s.id)">
        <div class="c-head">
          <span class="nm"><i class="dot" :class="s.online ? 'on' : 'off'" />{{ s.name }}</span>
          <span>
            <el-tag size="small" effect="plain">{{ MODE_TEXT[s.mode] }}</el-tag>
            <el-tag v-if="alarmsOf(s.id).length" size="small" type="danger" effect="dark" class="ml">{{ alarmsOf(s.id).length }}</el-tag>
          </span>
        </div>
        <div class="pipes">
          <div class="side-lbl muted">一次</div>
          <div class="num"><span class="supply">{{ fmt(s.ts1) }}</span> / <span class="return">{{ fmt(s.tr1) }}</span> ℃</div>
          <div class="num muted">{{ fmt(s.g1, 0) }} t/h</div>
          <div class="side-lbl muted">二次</div>
          <div class="num"><span class="supply">{{ fmt(s.ts2) }}</span> / <span class="return">{{ fmt(s.tr2) }}</span> ℃</div>
          <div class="num muted">设定 {{ fmt(s.ts2_set) }}</div>
        </div>
        <div class="valve">
          <span class="muted">阀门</span>
          <el-progress :percentage="+s.valve.toFixed(1)" :stroke-width="8" :color="s.valve > 95 ? '#ef4444' : '#f5a524'" />
        </div>
        <div class="foot">
          <span>热功率 <b class="num">{{ fmt(s.heat_mw, 2) }}</b> MW</span>
          <span>室温 <b class="num" :class="tinCls(s.tin)">{{ fmt(s.tin) }}</b> ℃</span>
        </div>
        <div v-if="alarmsOf(s.id).length" class="alarm-line">{{ alarmsOf(s.id)[0].message }}</div>
      </div>
    </div>

    <div v-else class="panel">
      <el-table :data="list" size="small" :default-sort="{ prop: 'id', order: 'ascending' }" @row-click="(r: any) => open(r.id)" class="clickable" height="calc(100vh - 190px)">
        <el-table-column prop="id" label="编号" width="72" sortable fixed />
        <el-table-column prop="name" label="站名" width="110" fixed>
          <template #default="{ row }"><i class="dot" :class="row.online ? 'on' : 'off'" />{{ row.name }}</template>
        </el-table-column>
        <el-table-column label="模式" width="84"><template #default="{ row }">{{ MODE_TEXT[row.mode] }}</template></el-table-column>
        <el-table-column v-for="c in cols" :key="c.k" :prop="c.k" :label="c.l" :width="c.w ?? 86" align="right" sortable>
          <template #default="{ row }"><span class="num" :class="c.cls?.(row[c.k])">{{ fmt(row[c.k], c.d ?? 1) }}</span></template>
        </el-table-column>
        <el-table-column label="报警" min-width="200">
          <template #default="{ row }"><span class="danger small">{{ alarmsOf(row.id).map((a) => a.message).join('；') }}</span></template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import type { StationRT } from '@/api/types'
import { useRealtime } from '@/stores/realtime'
import { fmt, MODE_TEXT } from '@/utils/format'

defineOptions({ name: 'StationList' })
const router = useRouter()
const { snap, meta } = storeToRefs(useRealtime())
const source = ref('')
const status = ref('')
const kw = ref('')
const view = ref<'card' | 'table'>('card')

const alarmsOf = (id: string) => snap.value!.alarms.filter((a) => a.object_id === id)
const tinCls = (t: number) => (t < 18.5 ? 'danger' : t > 22 ? 'warn' : 'ok')
const cardCls = (s: StationRT) => {
  if (!s.online) return 'offline'
  const ls = alarmsOf(s.id).map((a) => a.level)
  return ls.includes(1) ? 'lv1' : ls.includes(2) ? 'lv2' : ''
}
const open = (id: string) => router.push(`/stations/${id}`)

const list = computed(() =>
  Object.values(snap.value!.stations).filter((s) => {
    if (source.value && s.source_id !== source.value) return false
    if (kw.value && !(s.name.includes(kw.value) || s.id.toLowerCase().includes(kw.value.toLowerCase()))) return false
    if (status.value === 'alarm' && !alarmsOf(s.id).length) return false
    if (status.value === 'cold' && s.tin >= 18.5) return false
    if (status.value === 'hot' && s.tin <= 22) return false
    if (status.value === 'offline' && s.online) return false
    return true
  }),
)

const cols: { k: keyof StationRT; l: string; d?: number; w?: number; cls?: (v: any) => string }[] = [
  { k: 'ts1', l: '一次供温', cls: () => 'supply' },
  { k: 'tr1', l: '一次回温', cls: () => 'return' },
  { k: 'p1s', l: '一次供压', d: 3 },
  { k: 'p1r', l: '一次回压', d: 3 },
  { k: 'g1', l: '一次流量', d: 1 },
  { k: 'valve', l: '阀门%', d: 1 },
  { k: 'ts2', l: '二次供温', cls: () => 'supply' },
  { k: 'ts2_set', l: '供温设定' },
  { k: 'tr2', l: '二次回温', cls: () => 'return' },
  { k: 'p2s', l: '二次供压', d: 3 },
  { k: 'p2r', l: '二次回压', d: 3 },
  { k: 'g2', l: '二次流量', d: 0 },
  { k: 'heat_mw', l: '热功率MW', d: 2, w: 96 },
  { k: 'pump_freq', l: '泵频率Hz' , w: 92},
  { k: 'makeup_tph', l: '补水t/h', d: 2, cls: (v) => (v > 0.5 ? 'danger' : '') },
  { k: 'tin', l: '室温', cls: (v) => tinCls(v) },
]
</script>

<style scoped>
.filters { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 10px 14px; }
.counts b { color: var(--text); margin: 0 2px; }
.ml-auto { margin-left: auto; }
.ml { margin-left: 4px; }
.cards { grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); }
.card { cursor: pointer; padding: 12px; transition: border-color .2s, transform .2s; }
.card:hover { border-color: #3b8cff; transform: translateY(-1px); }
.card.lv1 { border-color: #7f1d1d; box-shadow: inset 0 0 0 1px #7f1d1d; }
.card.lv2 { border-color: #78501a; }
.card.offline { opacity: .6; }
.c-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.nm { font-weight: 600; display: flex; align-items: center; }
.pipes { display: grid; grid-template-columns: 34px 1fr auto; gap: 4px 6px; font-size: 13px; background: #0c1528; border-radius: 6px; padding: 6px 8px; }
.side-lbl { font-size: 12px; }
.valve { display: grid; grid-template-columns: 34px 1fr; align-items: center; gap: 6px; margin: 8px 0 4px; font-size: 12px; }
.foot { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); }
.foot b { color: var(--text); font-size: 15px; }
.foot b.danger { color: var(--danger); } .foot b.warn { color: var(--warn); } .foot b.ok { color: var(--ok); }
.alarm-line { margin-top: 6px; font-size: 12px; color: #fca5a5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.small { font-size: 12px; }
.clickable :deep(tr) { cursor: pointer; }
</style>
