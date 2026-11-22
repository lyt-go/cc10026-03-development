import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ReconciliationView from '@/views/ReconciliationView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 专用复核视图需放在动态模块路由之前，避免被 /:moduleKey 捕获
    { path: '/reconciliation', name: 'reconciliation', component: ReconciliationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
