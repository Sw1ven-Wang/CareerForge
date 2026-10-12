import { createRouter, createWebHistory } from 'vue-router'
import WorkbenchView from '../views/WorkbenchView.vue'
import RunsView from '../views/RunsView.vue'
import SourceLibraryView from '../views/SourceLibraryView.vue'
export default createRouter({ history: createWebHistory(), routes: [
  { path: '/', component: WorkbenchView },
  { path: '/runs', component: RunsView },
  { path: '/sources', component: SourceLibraryView },
] })
