<template>
  <el-container class="layout">
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
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="icons[m.icon]" /></el-icon>
          <template #title>
            {{ m.title }}
            <el-badge v-if="m.path === '/alarms' && rt.alarmCount" :value="rt.alarmCount" class="menu-badge" />
          </template>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header" height="52px">
        <div class="title">城市集中供热热网监控系统 <span class="muted">/ {{ route.meta.title }}</span></div>
        <div class="stats" v-if="rt.snap">
          <span>室外 <b class="num">{{ fmt(rt.snap.tout) }}</b>℃</span>
          <span>全网热负荷 <b class="num supply">{{ fmt(rt.snap.network.heat_mw) }}</b> MW</span>
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
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Bell, DataBoard, MagicStick, OfficeBuilding, Operation, Sunny, TrendCharts } from '@element-plus/icons-vue'
import { menus } from './router'
import { useRealtime } from './stores/realtime'
import { fmt } from './utils/format'

const icons: Record<string, any> = { DataBoard, Sunny, OfficeBuilding, TrendCharts, MagicStick, Operation, Bell }
const rt = useRealtime()
const route = useRoute()
const collapsed = ref(window.innerWidth < 900)
const active = computed(() => '/' + (route.path.split('/')[1] || 'overview'))
onMounted(() => rt.start())
</script>

<style scoped>
.layout { height: 100vh; }
.aside { background: #0c1528; border-right: 1px solid var(--border); transition: width .2s; overflow: hidden; }
.logo { height: 52px; display: flex; align-items: center; gap: 10px; padding: 0 18px; font-weight: 700; font-size: 16px; cursor: pointer; white-space: nowrap; border-bottom: 1px solid var(--border); }
.menu { border-right: 0; }
.menu :deep(.el-menu-item.is-active) { background: linear-gradient(90deg, #1d3a6b, transparent); border-left: 3px solid var(--accent); }
.menu-badge { margin-left: 8px; }
.header { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); background: #0c1528; gap: 12px; }
.title { font-size: 16px; font-weight: 600; white-space: nowrap; }
.stats { display: flex; align-items: center; gap: 18px; font-size: 13px; color: var(--muted); white-space: nowrap; overflow-x: auto; }
.stats b { color: var(--text); font-size: 15px; margin: 0 2px; }
.stats b.supply { color: var(--supply); }
.alarm-link { color: var(--text); display: flex; padding-top: 6px; }
.conn { display: flex; align-items: center; }
.main { padding: 0; overflow-y: auto; }
.loading { height: 60vh; }
@media (max-width: 1100px) { .clock { display: none; } .title .muted { display: none; } }
</style>
