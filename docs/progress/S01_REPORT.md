# S01 阶段报告

日期：2026-10-12。状态：工程骨架、身份、目标/来源版本及最小前端已完成本阶段验证。整个 V1 尚未完成。

## 实现

- FastAPI 应用与 live/ready；ready 检查数据库与已迁移身份表。
- Argon2 密码、服务端 Cookie 会话（令牌只存 SHA-256）、退出撤销、到期校验、会话 CSRF 与 Origin；登录尝试使用独立数据库事务记录并限频。
- CareerGoal/Version、Source/Snapshot、Citation、IdempotencyRecord、Deletion。复合外键拒绝跨 owner 关联；未知输入字段拒绝。
- 来源文本/链接线索导入，Unicode 左闭右开引用；原文/元数据更正建新快照。幂等重放先于版本前置条件且重新检查资源授权。
- 删除立即撤权并建立 revoked 清理记录。物理清理调度尚待 S02/S08；不冒称 completed。
- Vue3/TypeScript/Pinia/Element Plus 登录、工作台、职业目标与来源版本页面；提供窄屏布局。
- 初始 Alembic 迁移、OpenAPI 导出、生成 TypeScript 类型、Compose app profile、CI 配置。

## 实测

- 全新 PostgreSQL 数据库 careerforge_s01_verify 从空库升级 head 成功；alembic check 无 schema 漂移。
- pytest 全部 40 passed：26 模型离线契约 + 2 基础设施 + 12 S01 集成。JUnit 见 S01_TEST_RESULTS.xml。
- 两个独立账号：来源/版本/引用/删除/目标越权拒绝；注销与会话到期返回 401；缺 CSRF/错误 Origin 拒绝。
- 两线程、独立数据库连接的同键并发导入只产生一份快照；跨 owner 关联实际触发 PostgreSQL 外键拒绝。
- 历史引用继续指向旧原文；元数据类别更正不改历史快照；删除后旧键回放不能返回资源。
- Ruff 全通过，mypy 25 source files 无问题。
- npm install、OpenAPI 类型生成、vue-tsc 与 Vite production build 通过；npm audit 当次报告 0 vulnerabilities。
- docker compose --profile app config --quiet 通过。应用容器尚未 build/up，此阶段实际 UI 验收使用本机 API/Vite 与真实容器 PostgreSQL/Redis。
- 浏览器真实注册/登录、设置 Agent 目标、导入合成材料、打开原文快照成功。浏览器发现投入时间控件会显示默认 1，已修复并验证为空时显示“投入时间待补充”。合成材料明确标记 synthetic，不计入真实 JD 样本。

## 限制和下一阶段

Starlette TestClient 对 httpx 给出弃用提示；测试实际通过。前端整体 Element Plus 打包约 1MB，有 chunk size 提示，后续优化不影响当前正确性。
CI 文件已建立但未推送或执行 GitHub Actions。真实供应商验证按用户要求暂缓；模型效果与用户学习未验。
下一阶段 S02 用显式阶段函数实现真实 Celery Linux Worker、outbox、租约/epoch、阶段产物、等待决定、预算与事件回放，必须以故障测试验收。
