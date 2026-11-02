import { createRouter, createWebHistory } from 'vue-router'
import { useAuth } from '@/stores/auth'

/** perm：进入该页面需要的权限（任一）；没有权限的菜单不显示 */
export const menus = [
  { path: '/overview', title: '全网总览', icon: 'DataBoard', perm: ['monitor:read'] },
  { path: '/sources', title: '热源监控', icon: 'Sunny', perm: ['monitor:read'] },
  { path: '/stations', title: '热力站监控', icon: 'OfficeBuilding', perm: ['monitor:read'] },
  { path: '/analytics', title: '统计分析', icon: 'TrendCharts', perm: ['analytics:read'] },
  { path: '/ai', title: 'AI 运行分析', icon: 'MagicStick', perm: ['ai:read'] },
  { path: '/commands', title: '调控与审计', icon: 'Operation', perm: ['control:draft', 'control:approve', 'audit:read'] },
  { path: '/alarms', title: '报警中心', icon: 'Bell', perm: ['monitor:read'] },
  { path: '/agents', title: '智能体授权', icon: 'Key', perm: ['agent:delegate'] },
]

const permOf = (path: string) => menus.find((m) => path === m.path || path.startsWith(m.path + '/'))?.perm

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/login', component: () => import('@/views/Login.vue'), meta: { title: '登录', public: true } },
    { path: '/overview', component: () => import('@/views/Overview.vue'), meta: { title: '全网总览' } },
    { path: '/sources', component: () => import('@/views/SourceList.vue'), meta: { title: '热源监控' } },
    { path: '/sources/:id', component: () => import('@/views/SourceDetail.vue'), meta: { title: '热源详情' } },
    { path: '/stations', component: () => import('@/views/StationList.vue'), meta: { title: '热力站监控' } },
    { path: '/stations/:id', component: () => import('@/views/StationDetail.vue'), meta: { title: '热力站详情' } },
    { path: '/analytics', component: () => import('@/views/Analytics.vue'), meta: { title: '统计分析' } },
    { path: '/ai', component: () => import('@/views/AiAnalysis.vue'), meta: { title: 'AI 运行分析' } },
    { path: '/commands', component: () => import('@/views/Commands.vue'), meta: { title: '调控与审计' } },
    { path: '/alarms', component: () => import('@/views/Alarms.vue'), meta: { title: '报警中心' } },
    { path: '/agents', component: () => import('@/views/AgentTokens.vue'), meta: { title: '智能体授权' } },
    { path: '/forbidden', component: () => import('@/views/Forbidden.vue'), meta: { title: '无权访问' } },
  ],
})

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  const auth = useAuth()
  if (!auth.me && !(await auth.restore())) return { path: '/login', query: { redirect: to.fullPath } }
  const perm = permOf(to.path)
  if (perm && !auth.hasAny(...perm)) return { path: '/forbidden' }
  return true
})

export default router
