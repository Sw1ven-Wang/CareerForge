# CareerForge 1.6.0 · 规格功能补齐与可靠性完善版

CLI 优先的职业学习系统：可信知识生产、任务感知检索、逐块审核回答、学习闭环及验证后发布的策略进化。

> 本包沿用上传的独立 v1.5 实现并增量完善，**不是原 Vue/Tauri 工程的原地升级**。1.5.1 保留为可恢复检查点；1.6.0 的当前结果以 [交付报告](DELIVERY_REPORT.md)、[验证说明](docs/VALIDATION.md) 和 `validation/v16/final_verified/summary.json` 为准。历史日志不代表当前状态。

## 1.6.0 新增

- **分类治理**：提议、NamespaceAdmin 审批与 CAS、版本化 Topic/Facet、合并/拆分、严格类型和历史映射；进入真实 Analysis/Review/检索路径。
- **学习闭环**：授权个性化 Plan 提议/采纳、理解检查/练习/Rubric、独立尝试、观察与调整、可选复习建议。
- **进化回放**：安全教学模板、Teaching/Research/Router/Retrieval 实际双臂执行、独立 Gold 认证、校准、硬门禁、Canary/回滚；不足证据保持 inconclusive。
- **知识维护/公共任务**：固定快照 Diff、A/B/C 保守更新路线、明确授权的本地快照调度；系统付费公共构建 singleflight 与订阅/取消。
- **检索与渠道**：任务查询改写、批量向量索引/重建、PR/Release、ETag、跨 Worker 限流、权限与隐私同步缓存栅栏。
- **流式与治理**：一个完整块生成→Guard→提交→下一块；断线只重放；大图持久分页失效；个人导出/确认擦除与跨人派生副本精确清理。
- **旧库安全入口**：固定原仓库基线只读核对，按 owner 白名单只读导出和私人未审核归档导入；不继承旧审核、凭据或付款权力。

完整 [需求矩阵](docs/REQUIREMENTS_MATRIX.md) · [兼容边界](docs/ORIGIN_AND_COMPATIBILITY.md) · [剩余限制](docs/KNOWN_GAPS.md)。

## 快速验证

本次使用 CPython 3.12 / Node 24。新目录中执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints-python312-linux.txt '.[dev,production]' pytest-cov
npm ci --prefix coding-agent --ignore-scripts --no-audit --no-fund
python scripts/run_tests.py
python scripts/demo_offline.py
python -m mypy --no-incremental src/careerforge
```

报告默认写入新的 `validation/local/`，不覆盖随包证据；重复运行请指定新的 `--output-dir` 或 `--label`。离线脚本移除真实供应商配置，排除需显式授权的基础设施用例。Pi 使用实际 SDK/合成推理；演示不发真实 GitHub/付费模型请求。

## 使用入口

```bash
careerforge init --development
careerforge serve --development       # 一个终端
careerforge worker --development      # 另一个终端
careerforge chat --evidence-only      # CLI终端
careerforge resume                   # GET-only 恢复，不重付费
careerforge learning --help
careerforge replay --help
careerforge taxonomy --help
careerforge maintenance --help
careerforge shared --help
careerforge index --help
careerforge privacy --help
```

所有终端使用相同 `CAREERFORGE_STATE_DIR`。新库没有预置“已审核知识”；无资料时返回 Gap 正常。模型功能需要明确配置真实供应商、数据外发范围、资金及角色授权；本次真实付费调用数为 0。新控制面通过显式 JSON 合同与 revision/hash/idempotency 运行，不代填用户批准。见 [部署与操作](docs/DEPLOYMENT.md)。

## Docker 与验收边界

```bash
python scripts/verify_docker.py --confirm-docker-test
```

只有已具备受支持 Linux Docker daemon / cgroup / Compose 的授权环境才会构建及运行。当前 dot 已实际运行官方 Docker/Compose，配置解析通过；rootless 启动被 `newuidmap` 缺失等条件阻塞，**镜像构建、容器测试、隔离验收 not_run**。真实 PG/pgvector/Redis/Celery 原生进程测试不是 Docker 测试。详见 [Docker 证据](docs/DOCKER_VALIDATION.md)。

真实模型质量、人工 Gold、教学收益、生产性能/灾备和完整原工程兼容没有通过验收。工程测试与覆盖率不能替代这些结论。

## 升级

仅已确认来源、由本包 Alembic 管理的数据库可在备份及隔离演练后 `alembic upgrade head`（当前 `cf160_features`）。旧凭据/批准/知识审核不会通过迁移自动获得新权力。未标记库、错误 head、原 Vue/Tauri 旧库拒绝自动认领；可通过受限 `legacy-export` / `legacy-import` 私人归档，不可盲目 stamp。

交付不含 venv、node_modules、运行数据库、真实 `.env`、client.json 或系统二进制。`MANIFEST.sha256` 是完整性清单，不是数字签名。
