<template>
  <el-dialog v-model="visible" title="沉淀为处置案例" width="640px" :close-on-click-modal="false" destroy-on-close>
    <el-alert type="info" :closable="false" show-icon class="mb"
              title="提交后进入“待专家审核”，由值班调度长审核通过后才会出现在相似案例检索中。只记录已核实的处置过程。" />
    <div v-if="eventId" class="muted small mb">关联事件：{{ eventId }}（站点与报警代码由系统自动补全）</div>
    <el-form label-width="72px">
      <el-form-item label="标题"><el-input v-model="f.title" maxlength="80" show-word-limit /></el-form-item>
      <el-form-item label="现象"><el-input v-model="f.symptom" type="textarea" :rows="2" maxlength="500" /></el-form-item>
      <el-form-item label="原因"><el-input v-model="f.cause" placeholder="已核实的原因（可留空）" maxlength="300" /></el-form-item>
      <el-form-item label="处置步骤">
        <div class="steps">
          <div v-for="(s, i) in f.steps" :key="i" class="step">
            <span class="muted">{{ i + 1 }}.</span>
            <el-input v-model="f.steps[i]" maxlength="300" />
            <el-button link type="danger" :disabled="f.steps.length === 1" @click="f.steps.splice(i, 1)">删除</el-button>
          </div>
          <el-button size="small" @click="f.steps.push('')" :disabled="f.steps.length >= 20">+ 添加步骤</el-button>
        </div>
      </el-form-item>
      <el-form-item label="处置结果"><el-input v-model="f.outcome" maxlength="300" /></el-form-item>
      <el-form-item label="标签"><el-input v-model="f.tags" placeholder="用逗号分隔，如：泄漏, 二网" /></el-form-item>
    </el-form>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">提交审核</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/http'
import type { KbCase } from '@/api/types'

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ eventId?: string | null; objectId?: string | null; title?: string; symptom?: string; features?: string[] }>()
const emit = defineEmits<{ (e: 'saved', c: KbCase): void }>()
const f = reactive({ title: '', symptom: '', cause: '', steps: [''], outcome: '', tags: '' })
const busy = ref(false)
const error = ref('')

watch(visible, (v) => {
  if (!v) return
  Object.assign(f, { title: props.title ?? '', symptom: props.symptom ?? '', cause: '', steps: [''], outcome: '', tags: '' })
  error.value = ''
})

async function submit() {
  busy.value = true
  error.value = ''
  try {
    const r = await api.post<KbCase>('/api/kb/cases', {
      event_id: props.eventId ?? null, object_id: props.eventId ? null : props.objectId ?? null,
      title: f.title, symptom: f.symptom, cause: f.cause, steps: f.steps.filter((s) => s.trim()), outcome: f.outcome,
      features: props.features ?? [], tags: f.tags.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
    })
    ElMessage.success(r.summary)
    r.warnings.forEach((w) => ElMessage.warning(w))
    emit('saved', r.data)
    visible.value = false
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.small { font-size: 12px; }
.steps { width: 100%; display: flex; flex-direction: column; gap: 6px; }
.step { display: grid; grid-template-columns: 22px 1fr auto; gap: 6px; align-items: center; }
</style>
