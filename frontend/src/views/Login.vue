<template>
  <div class="login-page">
    <div class="card">
      <div class="brand">
        <svg viewBox="0 0 32 32" width="40" height="40">
          <path d="M16 3c4 6 8 9 8 15a8 8 0 0 1-16 0c0-3 1.5-5 3-7 0 3 1.5 5 3.5 5-1-4 0-9 1.5-13z" fill="#ff6b4a" />
          <path d="M16 18c2 2 3 3.5 3 5.5a3 3 0 0 1-6 0c0-2 1.5-3.5 3-5.5z" fill="#ffd166" />
        </svg>
        <div>
          <h1>城市集中供热热网监控系统</h1>
          <p class="muted">请使用平台账号登录</p>
        </div>
      </div>
      <el-alert v-if="auth.expiredMsg" :title="auth.expiredMsg" type="warning" show-icon :closable="false" class="mb" />
      <el-form @submit.prevent="submit" label-position="top">
        <el-form-item label="用户名">
          <el-input v-model="username" autocomplete="username" size="large" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" show-password autocomplete="current-password" size="large" @keyup.enter="submit" />
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb" />
        <el-button type="primary" size="large" class="full" :loading="busy" native-type="submit" @click="submit">登录</el-button>
      </el-form>

      <div v-if="demo.length" class="demo">
        <div class="demo-title">演示账号 <span class="muted">（点击直接登录；生产环境设置 HEATNET_DEMO=0 关闭）</span></div>
        <div class="grid demo-grid">
          <button v-for="a in demo" :key="a.username" class="acct" type="button" @click="quick(a)">
            <b>{{ a.name }}</b><span class="role">{{ a.role_name }}</span>
            <span class="muted small">{{ a.username }} · {{ a.scope }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getData } from '@/api/http'
import { useAuth } from '@/stores/auth'

interface Demo { username: string; name: string; role: string; role_name: string; scope: string; password: string }

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)
const demo = ref<Demo[]>([])

onMounted(async () => {
  try {
    demo.value = await getData<Demo[]>('/api/auth/demo-accounts')
  } catch {
    demo.value = []
  }
})

async function submit() {
  if (!username.value || !password.value) {
    error.value = '请输入用户名和密码'
    return
  }
  busy.value = true
  error.value = ''
  try {
    await auth.login(username.value.trim(), password.value)
    const to = (route.query.redirect as string) || '/overview'
    router.replace(to.startsWith('/login') ? '/overview' : to)
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
function quick(a: Demo) {
  username.value = a.username
  password.value = a.password
  submit()
}
</script>

<style scoped>
.login-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 16px;
  background: radial-gradient(circle at 30% 20%, #13264a, #0a1120 60%); }
.card { width: 100%; max-width: 520px; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 28px; }
.brand { display: flex; gap: 14px; align-items: center; margin-bottom: 20px; }
h1 { font-size: 20px; margin: 0; }
.brand p { margin: 4px 0 0; }
.mb { margin-bottom: 12px; }
.full { width: 100%; }
.demo { margin-top: 22px; border-top: 1px dashed var(--border); padding-top: 14px; }
.demo-title { font-size: 13px; margin-bottom: 10px; }
.demo-grid { grid-template-columns: 1fr 1fr; gap: 8px; }
.acct { text-align: left; background: #0c1528; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; color: var(--text);
  cursor: pointer; display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; font: inherit; }
.acct:hover { border-color: var(--accent); }
.role { font-size: 12px; color: #f5a524; }
.small { font-size: 12px; width: 100%; }
@media (max-width: 480px) { .demo-grid { grid-template-columns: 1fr; } }
</style>
