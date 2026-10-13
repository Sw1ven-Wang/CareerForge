import { createRouter, createWebHistory } from 'vue-router'
import LearningStudioView from '../views/LearningStudioView.vue'
import WorkbenchView from '../views/WorkbenchView.vue'
import MarketResearchView from '../views/MarketResearchView.vue'
import ProjectLearningView from '../views/ProjectLearningView.vue'
import PlanningView from '../views/PlanningView.vue'
import MockResearchView from '../views/MockResearchView.vue'
import RunsView from '../views/RunsView.vue'
import SourceLibraryView from '../views/SourceLibraryView.vue'
export default createRouter({ history: createWebHistory(), routes: [
  { path: '/', component: WorkbenchView },
  { path: '/build', redirect: '/planning' },
  { path: '/studio', component: LearningStudioView },
  { path: '/projects', component: ProjectLearningView },
  { path: '/planning', component: PlanningView },
  { path: '/mock-research', component: MockResearchView },
  { path: '/market', component: MarketResearchView },
  { path: '/runs', component: RunsView },
  { path: '/sources', component: SourceLibraryView },
] })
