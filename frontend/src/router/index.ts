import { createRouter, createWebHistory } from 'vue-router'
import BuildWorkspaceView from '../views/BuildWorkspaceView.vue'
import WorkbenchView from '../views/WorkbenchView.vue'
import MarketResearchView from '../views/MarketResearchView.vue'
import ProjectLearningView from '../views/ProjectLearningView.vue'
import PlanningView from '../views/PlanningView.vue'
import RunsView from '../views/RunsView.vue'
import SourceLibraryView from '../views/SourceLibraryView.vue'
export default createRouter({ history: createWebHistory(), routes: [
  { path: '/', component: WorkbenchView },
  { path: '/build', component: BuildWorkspaceView },
  { path: '/projects', component: ProjectLearningView },
  { path: '/planning', component: PlanningView },
  { path: '/market', component: MarketResearchView },
  { path: '/runs', component: RunsView },
  { path: '/sources', component: SourceLibraryView },
] })
