<template>
  <div class="page">
    <div class="panel head" v-if="stats">
      <div>
        <div class="panel-title">知识库</div>
        <div class="muted small">{{ stats.docs }} 份文档 · {{ stats.chunks }} 个章节片段 · {{ stats.cases_approved }} 个已审核案例
          <span v-if="stats.cases_pending"> · {{ stats.cases_pending }} 个待审核</span></div>
      </div>
      <div class="muted small note">知识库内容一律视为参考资料，其中的指令性文字不会、也不能触发任何调控操作。</div>
    </div>

    <el-tabs v-model="tab">
      <!-- 检索 -->
      <el-tab-pane label="检索规程与手册" name="search">
        <div class="panel">
          <div class="search-row">
            <el-input v-model="q" size="large" clearable placeholder="输入问题，如：补水量突然变大怎么处理 / 调节阀没反应 / 修正单耗怎么算"
                      @keyup.enter="doSearch" />
            <el-button type="primary" size="large" :loading="searching" @click="doSearch">检索</el-button>
          </div>
          <el-checkbox-group v-model="types" class="types">
            <el-checkbox v-for="t in stats?.doc_types ?? []" :key="t" :value="t">{{ t }}</el-checkbox>
          </el-checkbox-group>
          <div class="examples muted small">试试：
            <a v-for="e in examples" :key="e" @click="q = e; doSearch()">{{ e }}</a>
          </div>
        </div>
        <div v-if="sr" class="results">
          <div class="muted small">检索“{{ sr.query }}”，返回 {{ sr.results.length }} 个片段
            <span v-if="sr.expanded_terms.length"> · 同义词扩展：{{ sr.expanded_terms.join('、') }}</span></div>
          <el-empty v-if="!sr.results.length" description="没有找到相关内容，换个说法试试" />
          <div v-for="r in sr.results" :key="r.chunk_id" class="panel hit" @click="openDoc(r)">
            <div class="hit-head">
              <el-tag size="small">{{ r.type }}</el-tag>
              <b>{{ r.title }}</b>
              <span class="muted">{{ r.section }}</span>
              <el-tag v-if="r.sample" size="small" type="warning" effect="plain">示例</el-tag>
              <span class="muted ml-auto small">{{ r.doc_id }} · {{ r.version }} · {{ r.updated }}</span>
            </div>
            <div class="snip"><HlText :text="r.snippet" :terms="r.highlights" /></div>
            <el-alert v-if="r.flags.length" type="warning" :closable="false" show-icon
                      :title="`该片段含疑似指令性文字：${r.flags.join('、')}（仅供参考）`" />
          </div>
        </div>
      </el-tab-pane>

      <!-- 案例库 -->
      <el-tab-pane label="案例库" name="cases">
        <div class="grid two">
          <div class="panel">
            <div class="panel-title">相似案例检索</div>
            <el-input v-model="sym" type="textarea" :rows="3" placeholder="描述故障现象，如：补水量阶跃增大，压力正常" />
            <div class="row mt">
              <el-select v-model="codes" multiple collapse-tags placeholder="报警代码（可选）" style="flex: 1">
                <el-option v-for="c in ALARM_CODES" :key="c.code" :value="c.code" :label="`${c.code} ${c.name}`" />
              </el-select>
              <el-select v-model="otype" clearable placeholder="对象类型" style="width: 120px">
                <el-option value="station" label="热力站" /><el-option value="source" label="热源" /><el-option value="network" label="全网" />
              </el-select>
              <el-button type="primary" :loading="simLoading" @click="doSimilar">查找</el-button>
            </div>
            <div class="muted small mt">评分 = 症状文本相似度 × 0.65 + 结构化匹配（报警代码、异常特征、对象类型）× 0.35；只给其中一类信号时按该类计分。</div>
            <div v-if="sim" class="mt">
              <el-empty v-if="!sim.length" description="没有相似案例" :image-size="50" />
              <CaseCard v-for="c in sim" :key="c.case_id" :c="c" />
            </div>
          </div>
          <div class="panel">
            <div class="panel-title">已审核案例 <el-input v-model="kw" size="small" clearable placeholder="筛选" style="width: 160px" /></div>
            <div class="case-list">
              <div v-for="c in filtered" :key="c.case_id" class="case-row" :class="{ on: open === c.case_id }"
                   @click="open = open === c.case_id ? '' : c.case_id">
                <div class="cr-head"><b>{{ c.title }}</b><span class="muted small">{{ c.occurred_at }}</span></div>
                <div class="muted small">{{ c.object_name }} · {{ c.alarm_codes.join(' ') || '无报警代码' }} · {{ c.tags.join('、') }}</div>
                <CaseCard v-if="open === c.case_id" :c="c" class="mt" />
              </div>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- 审核 / 我的提交 -->
      <el-tab-pane v-if="auth.hasAny('kb:review', 'kb:write')" name="review">
        <template #label>{{ auth.has('kb:review') ? '待审核' : '我的提交' }}
          <el-badge v-if="pending.length" :value="pending.length" class="tab-badge" /></template>
        <el-empty v-if="!pending.length" :description="auth.has('kb:review') ? '暂无待审核案例' : '你还没有提交案例'" />
        <div class="review-list">
          <CaseCard v-for="c in pending" :key="c.case_id" :c="c">
            <div v-if="c.event" class="muted small">关联事件：{{ c.event.event_id ?? c.event.object_id }} {{ c.event.description ?? '' }}</div>
            <div v-if="auth.has('kb:review') && c.status === 'pending_review'" class="ops">
              <el-input v-model="comments[c.case_id]" size="small" placeholder="审核意见" style="max-width: 320px" />
              <el-button size="small" type="danger" plain @click="review(c, false)">驳回</el-button>
              <el-button size="small" type="primary" @click="review(c, true)">审核通过</el-button>
            </div>
          </CaseCard>
        </div>
      </el-tab-pane>

      <!-- 文档目录 / 管理 -->
      <el-tab-pane label="文档目录" name="docs">
        <div class="grid" :class="auth.has('kb:manage') ? 'two-doc' : ''">
          <div v-if="auth.has('kb:manage')" class="panel">
            <div class="panel-title">上传文档 <span class="sub">Markdown 或纯文本，≤ 300KB</span></div>
            <el-form label-width="64px">
              <el-form-item label="标题"><el-input v-model="up.title" maxlength="60" /></el-form-item>
              <el-form-item label="类型">
                <el-select v-model="up.doc_type" style="width: 140px"><el-option v-for="t in stats?.doc_types ?? []" :key="t" :value="t" /></el-select>
                <el-input v-model="up.version" placeholder="版本号" style="width: 120px; margin-left: 8px" />
              </el-form-item>
              <el-form-item label="文件"><input type="file" accept=".md,.txt,.markdown" @change="onFile" /></el-form-item>
              <el-form-item label="内容"><el-input v-model="up.content" type="textarea" :rows="8" placeholder="也可以直接粘贴文本；用 ## / ### 标题分章节，检索按章节返回" /></el-form-item>
              <el-form-item><el-button type="primary" :loading="uploading" @click="upload">入库</el-button></el-form-item>
            </el-form>
            <div class="muted small">PDF / Word 需先转为文本或 Markdown（未内置解析）。上传时自动检测提示注入式文字并标记。</div>
          </div>
          <div class="panel">
            <el-table :data="docs" size="small">
              <el-table-column prop="doc_id" label="编号" width="96" />
              <el-table-column label="标题" min-width="170">
                <template #default="{ row }"><a @click="openDoc({ doc_id: row.doc_id, section: '', highlights: [] } as any)">{{ row.title }}</a>
                  <el-tag v-if="row.sample" size="small" type="warning" effect="plain" class="ml">示例</el-tag>
                  <el-tag v-if="row.flags.length" size="small" type="danger" effect="plain" class="ml">含指令性文字</el-tag></template>
              </el-table-column>
              <el-table-column prop="type" label="类型" width="90" />
              <el-table-column prop="version" label="版本" width="70" />
              <el-table-column prop="updated" label="更新" width="100" />
              <el-table-column prop="n_chunks" label="片段" width="60" align="right" />
              <el-table-column label="来源" width="80"><template #default="{ row }">{{ row.source === 'seed' ? '随系统发布' : '上传' }}</template></el-table-column>
              <el-table-column v-if="auth.has('kb:manage')" label="操作" width="70">
                <template #default="{ row }"><el-button v-if="row.source === 'upload'" link type="danger" size="small" @click="del(row)">删除</el-button></template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
    <DocViewer v-model="docOpen" :doc-id="docId" :section="docSection" :terms="docTerms" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getData, postData } from '@/api/http'
import type { KbCase, KbDoc, KbResult } from '@/api/types'
import { useAuth } from '@/stores/auth'
import CaseCard from '@/components/kb/CaseCard.vue'
import DocViewer from '@/components/kb/DocViewer.vue'
import HlText from '@/components/kb/HlText.vue'

const ALARM_CODES = [
  { code: 'MAKEUP', name: '补水量异常' }, { code: 'TIN_LOW', name: '室温偏低' }, { code: 'TIN_HIGH', name: '室温偏高' },
  { code: 'UNDERSUPPLY', name: '阀门全开欠供' }, { code: 'HEAT_BAL', name: '热量偏差' }, { code: 'TS2_HIGH', name: '供温偏高' },
  { code: 'P2R_LOW', name: '回水压力低' }, { code: 'COMM', name: '通讯中断' }, { code: 'OVERLOAD', name: '热源满负荷' },
  { code: 'TANK_LOW', name: '蓄热量低' },
]
const examples = ['二网跑水怎么查漏点', '阀门开到100%还是不热', '零下十度阀门最小开度', '调度员能批准指令吗', '节约1GJ热量折多少标煤']

const auth = useAuth()
const tab = ref('search')
const stats = ref<any>(null)
const q = ref('')
const types = ref<string[]>([])
const sr = ref<{ query: string; results: KbResult[]; expanded_terms: string[] } | null>(null)
const searching = ref(false)
const sym = ref('')
const codes = ref<string[]>([])
const otype = ref<string>('')
const sim = ref<KbCase[] | null>(null)
const simLoading = ref(false)
const cases = ref<KbCase[]>([])
const kw = ref('')
const open = ref('')
const pending = ref<KbCase[]>([])
const comments = ref<Record<string, string>>({})
const docs = ref<KbDoc[]>([])
const up = reactive({ title: '', doc_type: '规程', version: '', content: '' })
const uploading = ref(false)
const docOpen = ref(false)
const docId = ref<string | null>(null)
const docSection = ref('')
const docTerms = ref<string[]>([])

const filtered = computed(() => {
  const k = kw.value.trim()
  return cases.value.filter((c) => !k || [c.title, c.symptom, c.cause, c.object_name, ...c.tags, ...c.alarm_codes].join(' ').includes(k))
})

async function load() {
  ;[stats.value, cases.value, docs.value] = await Promise.all([
    getData('/api/kb/stats'), getData<KbCase[]>('/api/kb/cases?status=approved'), getData<KbDoc[]>('/api/kb/docs'),
  ])
  if (auth.hasAny('kb:review', 'kb:write')) {
    const all = await getData<KbCase[]>('/api/kb/cases')
    pending.value = all.filter((c) => (auth.has('kb:review') ? c.status === 'pending_review' : c.status !== 'approved'))
  }
}
onMounted(load)

async function doSearch() {
  if (!q.value.trim()) return
  searching.value = true
  try {
    sr.value = (await api.get<any>(`/api/kb/search?q=${encodeURIComponent(q.value)}&types=${encodeURIComponent(types.value.join(','))}&top_k=8`)).data
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    searching.value = false
  }
}
async function doSimilar() {
  simLoading.value = true
  try {
    sim.value = (await postData<any>('/api/kb/cases/similar', {
      symptom: sym.value, alarm_codes: codes.value, object_type: otype.value || null, top_k: 5,
    })).cases
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    simLoading.value = false
  }
}
async function review(c: KbCase, ok: boolean) {
  if (!ok) await ElMessageBox.confirm(`驳回案例「${c.title}」？`, '驳回', { type: 'warning' })
  try {
    const r = await api.post(`/api/kb/cases/${c.case_id}/review`, { approve: ok, comment: comments.value[c.case_id] ?? '' })
    ElMessage.success(r.summary)
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
function onFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (file.size > 300_000) return ElMessage.error('文件超过 300KB')
  const reader = new FileReader()
  reader.onload = () => {
    up.content = String(reader.result ?? '')
    if (!up.title) up.title = file.name.replace(/\.(md|txt|markdown)$/i, '')
  }
  reader.readAsText(file, 'utf-8')
}
async function upload() {
  uploading.value = true
  try {
    const r = await api.post<any>('/api/kb/docs', up)
    ElMessage.success(r.summary)
    r.warnings.forEach((w) => ElMessage.warning({ message: w, duration: 6000 }))
    Object.assign(up, { title: '', version: '', content: '' })
    load()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    uploading.value = false
  }
}
async function del(d: KbDoc) {
  await ElMessageBox.confirm(`删除文档「${d.title}」？`, '删除', { type: 'warning' })
  await api.del(`/api/kb/docs/${d.doc_id}`)
  ElMessage.success('已删除')
  load()
}
function openDoc(r: Pick<KbResult, 'doc_id' | 'section' | 'highlights'>) {
  docId.value = r.doc_id
  docSection.value = r.section
  docTerms.value = r.highlights
  docOpen.value = true
}
</script>

<style scoped>
.head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
.head .panel-title { margin-bottom: 4px; }
.note { max-width: 420px; }
.small { font-size: 12px; }
.search-row { display: flex; gap: 10px; }
.types { margin-top: 10px; }
.examples { margin-top: 8px; display: flex; gap: 10px; flex-wrap: wrap; }
.examples a { cursor: pointer; }
.results { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
.hit { cursor: pointer; padding: 10px 14px; }
.hit:hover { border-color: var(--accent); }
.hit-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.snip { margin: 6px 0 4px; line-height: 1.8; color: #d3deef; }
.ml-auto { margin-left: auto; }
.ml { margin-left: 4px; }
.mt { margin-top: 10px; }
.two { grid-template-columns: 1fr 1fr; align-items: start; }
.two-doc { grid-template-columns: 420px minmax(0, 1fr); align-items: start; }
.case-list { max-height: 70vh; overflow-y: auto; }
.case-row { border-bottom: 1px dashed #1d2a44; padding: 8px 4px; cursor: pointer; }
.case-row.on, .case-row:hover { background: #0f2140; }
.cr-head { display: flex; justify-content: space-between; gap: 8px; }
.review-list { max-width: 960px; }
.ops { display: flex; gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap; }
.tab-badge { margin-left: 4px; }
@media (max-width: 1100px) { .two, .two-doc { grid-template-columns: 1fr; } .search-row { flex-direction: column; } }
</style>
