<template>
  <router-view v-if="route.meta.public" />
  <el-container v-else class="layout">
    <el-aside :width="collapsed ? '64px' : '196px'" class="aside">
      <div class="logo" @click="collapsed = !collapsed">
        <svg viewBox="0 0 32 32" width="28" height="28">
          <path d="M16 3c4 6 8 9 8 15a8 8 0 0 1-16 0c0-3 1.5-5 3-7 0 3 1.5 5 3.5 5-1-4 0-9 1.5-13z" fill="#ff6b4a" />
          <path d="M16 18c2 2 3 3.5 3 5.5a3 3 0 0 1-6 0c0-2 1.5-3.5 3-5.5z" fill="#ffd166" />
        </svg>
        <span v-if="!collapsed">智慧热网</span>
      </div>
      <el-menu :default-active="active" :collapse="collapsed" router background-color="transparent"
               text-color="#8797b3" active-text-color="#ffffff" class="menu">
        <el-menu-item v-for="m in visibleMenus" :key="m.path" :index="m.path">
          <el-icon><component :is="icons[m.icon]" /></el-icon>
          <template #title>
            {{ m.title }}
            <el-badge v-if="m.path === '/alarms' && rt.alarmCount" :value="rt.alarmCount" class="menu-badge" />
            <el-badge v-if="m.path === '/commands' && rt.snap?.pending_approvals" :value="rt.snap.pending_approvals" type="warning" class="menu-badge" />
          </template>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header" height="52px">
        <div class="title">城市集中供热热网监控系统 <span class="muted">/ {{ route.meta.title }}</span>
          <el-tag v-if="rt.meta?.scope && !rt.meta.scope.global" size="small" type="warning" effect="plain" class="scope-tag">
            数据范围：{{ rt.meta.scope.name }}</el-tag>
        </div>
        <div class="stats" v-if="rt.snap">
          <span>室外 <b class="num">{{ fmt(rt.snap.tout) }}</b>℃</span>
          <span>热负荷 <b class="num supply">{{ fmt(rt.snap.network.heat_mw) }}</b> MW</span>
          <span>平均室温 <b class="num">{{ fmt(rt.snap.network.tin_avg) }}</b>℃</span>
          <span>站点在线 <b class="num">{{ rt.snap.network.online }}/{{ rt.snap.network.total }}</b></span>
          <router-link to="/alarms" class="alarm-link">
            <el-badge :value="rt.alarmCount" :hidden="!rt.alarmCount" type="danger">
              <el-icon :size="18"><Bell /></el-icon>
            </el-badge>
          </router-link>
          <span class="conn"><i class="dot" :class="rt.connected ? 'on' : 'off'" />{{ rt.connected ? '实时' : '轮询' }}</span>
          <span class="num muted clock">{{ rt.snap.ts.replace('T', ' ') }}</span>
        </div>
        <el-dropdown v-if="auth.me" trigger="click" @command="onUser">
          <span class="user">
            <el-icon><UserFilled /></el-icon>{{ auth.me.name }}<span class="muted role">{{ auth.me.role_name }}</span>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>{{ auth.me.username }} · {{ auth.me.scope.global ? '全网' : rt.meta?.scope?.name }}</el-dropdown-item>
              <el-dropdown-item v-if="auth.has('agent:delegate')" command="agents">智能体授权</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main class="main">
        <div v-if="!rt.snap" class="loading" v-loading="true" element-loading-text="正在连接热网数据服务…" />
        <router-view v-else v-slot="{ Component }">
          <keep-alive :include="['Overview', 'StationList', 'SourceList']">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Bell, DataBoard, Document, Grid, Headset, Key, MagicStick, OfficeBuilding, Operation, Reading, SetUp, Sunny, TrendCharts, UserFilled } from '@element-plus/icons-vue'
import { menus } from './router'
import { useAuth } from './stores/auth'
import { useRealtime } from './stores/realtime'
import { fmt } from './utils/format'

const icons: Record<string, any> = { DataBoard, Grid, Sunny, OfficeBuilding, TrendCharts, MagicStick, Operation, Bell, SetUp, Headset, Document, Key, Reading }
const rt = useRealtime()
const auth = useAuth()
const route = useRoute()
const router = useRouter()
const collapsed = ref(window.innerWidth < 900)
const active = computed(() => '/' + (route.path.split('/')[1] || 'overview'))
const visibleMenus = computed(() => menus.filter((m) => auth.hasAny(...m.perm)))

// 登录后启动实时数据；退出或令牌失效时清空并回到登录页
watch(
  () => auth.me?.username,
  (u) => {
    rt.stop()
    if (u) rt.start()
  },
  { immediate: true },
)
watch(
  () => auth.expiredMsg,
  (m) => {
    if (m) {
      ElMessage.warning(m)
      router.replace({ path: '/login', query: { redirect: route.fullPath } })
    }
  },
)

async function onUser(cmd: string) {
  if (cmd === 'agents') router.push('/agents')
  if (cmd === 'logout') {
    await auth.logout()
    router.replace('/login')
  }
}
</script>

<style scoped>
.layout { height: 100vh; }
.aside { background: #0c1528; border-right: 1px solid var(--border); transition: width .2s; overflow: hidden; }
.logo { height: 52px; display: flex; align-items: center; gap: 10px; padding: 0 18px; font-weight: 700; font-size: 16px; cursor: pointer; white-space: nowrap; border-bottom: 1px solid var(--border); }
.menu { border-right: 0; }
.menu :deep(.el-menu-item.is-active) { background: linear-gradient(90deg, #1d3a6b, transparent); border-left: 3px solid var(--accent); }
.menu-badge { margin-left: 8px; }
.header { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); background: #0c1528; gap: 12px; }
.title { font-size: 16px; font-weight: 600; white-space: nowrap; display: flex; align-items: center; gap: 8px; }
.scope-tag { font-weight: 400; }
.user { display: flex; align-items: center; gap: 6px; cursor: pointer; color: var(--text); white-space: nowrap; outline: none; }
.user .role { font-size: 12px; }
.stats { display: flex; align-items: center; gap: 18px; font-size: 13px; color: var(--muted); white-space: nowrap; overflow-x: auto; }
.stats b { color: var(--text); font-size: 15px; margin: 0 2px; }
.stats b.supply { color: var(--supply); }
.alarm-link { color: var(--text); display: flex; padding-top: 6px; }
.conn { display: flex; align-items: center; }
.main { padding: 0; overflow-y: auto; }
.loading { height: 60vh; }
@media (max-width: 1100px) { .clock { display: none; } .title .muted { display: none; } }
</style>
