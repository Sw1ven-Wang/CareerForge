# 验收记录｜2026-10-29

## 1. 裁决

**离线工程测试通过；完整 v1.5 产品验收未通过。** 原因同时包含未完成规格（`KNOWN_GAPS.md`）和缺失的真实外部集成/质量验证，而不只是缺模型密钥。

本次没有降低原规格，也没有把测试供应商替身标成真实推理。验收脚本、原始 JUnit 与统计一并交付。

## 2. 本次实际执行

| 项目 | 实际结果 | 证据 |
|---|---|---|
| 所有收集到的离线用例 | **83 passed / 0 failed / 0 error / 0 skipped** | `validation/pytest.xml`、`batch1/2/3.xml` |
| 执行方式 | 三个互不重复子进程批次；实际覆盖全部 collect 列表 | `collected_tests.txt`、`summary.json`、各 `batch*_run.json` |
| 分批数量 | 27 + 29 + 27 | JUnit 与命令日志 |
| 语句覆盖 | **4,280 / 5,457 = 78.43%** | `coverage.json`，不是分支覆盖或模型准确率 |
| Python 语法编译 | `compileall src scripts alembic deploy` 通过 | 最终工具运行；不替代类型/风格检查 |
| Node 语法 | `node --check coding-agent/runner.mjs` 通过 | 不代表真实 Pi SDK 可用 |
| Wheel 构建 | pip wheel 成功 | `wheel_build.log`、`dist/` |
| 独立目录安装 wheel | install / CLI help / SQLite init 全部退出0 | `wheel_install.json`、`wheel_install.log` |
| 数据模型与种子 | 新建60表、30个Topic、0个Publication | wheel 安装检查；不预灌正式知识 |
| OpenAPI | 导出62个 path | `docs/openapi.json` |
| 实际网络层 | 本地 Uvicorn TCP + 独立 CLI subprocess + SSE | `test_real_loopback_cli_chat_sources_and_feedback` |
| 离线业务演示 | JD→研究树→分析→独立审核→发布→问答→反馈 | `offline_demo.json`，明确 synthetic |
| 生产预检 | **blocked，退出2** | `environment.json`、`doctor.log` |
| 生产集成验证脚本 | **blocked，退出2** | `production.json`、`production_attempt.log` |
| Ruff / mypy | 未执行，本环境未安装 | 不报告“lint/type全部通过” |
| GitHub Actions | 提供 workflow，未提交/触发远程运行 | 不报告“CI全绿” |

安装测试从临时目录加载构建 wheel，不使用源码 PYTHONPATH；依赖使用本环境已安装包。它不是全新联网环境的完整依赖解析测试。

## 3. 正向流程

本次工程用例实际走服务代码与数据库，而非只验证模型 JSON 外形：

1. 需求原文保存与提取 → 规划真实的持久逻辑节点 → 查询观测 → Reflector 新节点 → 固定来源。
2. Analysis 自己的读取会话 → 知识草稿/Claim → Review 独立读取 → 冻结取证包 → 对照审核 → Publication。
3. 自然语言意图 → 自动项目/技术范围 → 任务选通道 → 分组 EvidencePack → Tutor/Guard → 持久SSE → CLI。
4. 反馈授权 → Experience → 候选 → 组隔离 Holdout 配对评测 → CAS 晋升 → 下一轮冻结新版本 → Canary/回滚。
5. 工作区 → 待审补丁 → APPLY → 独立命令批准 → 执行身份绑定 → 合成沙箱回执 → 提交/自验。

模型文本、GitHub观测、语义向量和命令运行回执在这些路径中使用明确的 fixture。真实模型质量、真实 GitHub 可用性、实际资源隔离和程序正确性不因此自动成立。

## 4. 反向验收覆盖

已测：缓存热命中后权限撤销、私人回答依赖公共知识、公共影响不泄露私人消费者、概念 SourceSet 更换不产生多个默认版本、TopicLink 失效不整仓撤销、伪造 Fact、错误项目引用、条件漂移、普通改写越界、高风险事实、隐藏在 gap 的项目事实、审核失败不得发布、未知付费请求不得自动重试、旧响应回放仍需校验、父子预算共享、在途取消、租约失效、独立尝试暂停辅助、旧补丁冲突、命令版本冲突、无宿主 Shell 兜底、经验撤回、Gold 污染、候选篡改、基线CAS变化、灰度退化、旧库保护等。

这些是指定样例的工程结果，不是形式化安全证明。

## 5. 本次发现并修复的缺陷

- Coding 使用旧 Run 对象提交导致事件序号竞争，改为提交前重新取得有效租约对象。
- 父任务等待/子任务完成竞态可能造成错过唤醒，调整等待检查。
- 模型已保存响应的回放仍需执行 Schema/终止原因/token 限制校验。
- 热缓存必须重新检查共享/私有授权与 Publication 权威状态。
- Alembic inspect 自动开启事务导致版本标记未提交，迁移事务开始前完成该事务。
- Pydantic 全局 trim 和 CLI 文本读取可能改变原始源码空白/换行，改为保留字节解码内容。
- 不允许 CLI 未核实许可证就默认 MIT。
- 命令执行端失联只标 unknown，不自动再次执行。
- bootstrap 与迁移都拒绝原项目数据库，避免独立实现污染旧数据。

此前被执行环境中断的覆盖率命令保留在 `validation/attempts/`，没有计入通过结论；最终使用三批全部完成、退出0的报告。

## 6. 重现

```bash
python -m pip install -e '.[dev]' pytest-cov
python scripts/run_tests.py --coverage
# 在单次命令执行时间受限的环境，可按同样三批执行：
python scripts/run_tests.py --coverage --label batch1 tests/test_adapters.py tests/test_api.py tests/test_coding.py tests/test_runtime.py
python scripts/run_tests.py --coverage --append-coverage --label batch2 tests/test_evolution.py tests/test_knowledge_security.py
python scripts/run_tests.py --coverage --append-coverage --label batch3 tests/test_learning_migrations.py tests/test_lifecycle_rollout.py tests/test_retrieval_guard.py tests/test_smoke.py
python scripts/demo_offline.py
python -m pip wheel . --no-deps --wheel-dir dist
python scripts/verify_wheel.py
```

`--coverage` 全量运行重新建立覆盖率；分批时只有第一批清空，后两批 append。不要只跑最后一批就报告整体覆盖率。

## 7. 真实验收仍需哪些证据

PG/pgvector 实际 migration/索引/权限/并发；Redis 多进程 singleflight 与宕机；Celery Worker SIGKILL/接管；真实模型与工具轮次；真实 Embedding/Reranker 的同语料消融；Pi 1.3.0 实际 SDK 会话；Docker 进程/资源/网络边界；独立人工 Gold 的 Analysis/Review/Guard 错误率；真实教学与迁移样本；性能与费用记录。

本次没有这些运行条件或资料，因此所有相关产品指标保持未知。不得将 `synthetic=true` 改掉后视为完成。
