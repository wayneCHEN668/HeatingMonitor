import { createRouter, createWebHistory } from 'vue-router'

export const menus = [
  { path: '/overview', title: '全网总览', icon: 'DataBoard' },
  { path: '/sources', title: '热源监控', icon: 'Sunny' },
  { path: '/stations', title: '热力站监控', icon: 'OfficeBuilding' },
  { path: '/analytics', title: '统计分析', icon: 'TrendCharts' },
  { path: '/ai', title: 'AI 运行分析', icon: 'MagicStick' },
  { path: '/commands', title: '调控与审计', icon: 'Operation' },
  { path: '/alarms', title: '报警中心', icon: 'Bell' },
]

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', component: () => import('@/views/Overview.vue'), meta: { title: '全网总览' } },
    { path: '/sources', component: () => import('@/views/SourceList.vue'), meta: { title: '热源监控' } },
    { path: '/sources/:id', component: () => import('@/views/SourceDetail.vue'), meta: { title: '热源详情' } },
    { path: '/stations', component: () => import('@/views/StationList.vue'), meta: { title: '热力站监控' } },
    { path: '/stations/:id', component: () => import('@/views/StationDetail.vue'), meta: { title: '热力站详情' } },
    { path: '/analytics', component: () => import('@/views/Analytics.vue'), meta: { title: '统计分析' } },
    { path: '/ai', component: () => import('@/views/AiAnalysis.vue'), meta: { title: 'AI 运行分析' } },
    { path: '/commands', component: () => import('@/views/Commands.vue'), meta: { title: '调控与审计' } },
    { path: '/alarms', component: () => import('@/views/Alarms.vue'), meta: { title: '报警中心' } },
  ],
})
