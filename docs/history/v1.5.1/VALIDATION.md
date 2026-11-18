# 1.5.1 最终验证｜2026-11-02

## 结果与证据

- **376项离线工程用例通过**，无失败/错误/跳过；7项production用例在这一轮明确排除。
- **7项真实PG/Redis用例通过**，无失败/错误/跳过。383项采集节点与两份最终JUnit逐一对账，无遗漏/重叠。
- **真实基础设施验证器通过**：空库保护、Alembic、pgvector SQL、Redis跨客户端锁、Outbox→Celery→Worker→PG、无真实模型调用。
- **Ruff lint/格式、Python编译、Node语法、pip check、wheel构建/独立安装通过**。
- **mypy项目检查通过**：41个源文件，0诊断；最终以`--no-incremental`重新检查。没有全局缺失导入忽略；仅Celery未提供类型标记的外部import有精确单行豁免。
- **Docker blocked**：仅官方CLI/Compose配置解析通过；镜像、容器、隔离测试未运行。
- 语句覆盖：5,454/6,516，83.70%。真实外部进程/Worker不纳入这份离线覆盖率，不代表未执行；也不能把覆盖率当模型质量。

权威索引：`../validation/current/summary.json`；执行命令/退出码在`final_offline_run.json`、`static_checks.json`，JUnit在`final_offline.xml`、`final_native_authorized.xml`。

`validation/current/`中包含修复前故障与中间试跑，不能据此把最终修复误认为仍失败，也不能抹掉它们。首次最终native试跑因未开启两个测试opt-in开关而有6项skip；随后明确启用开关，`final_native_authorized.xml`记录全部7项真实通过。只有上述最终命名的证据用于383项统计。

类型修复前的135条诊断、首次生产报告目录覆写保护退出，分别保留在`pre_type_fixes/`和`pre_facet_fix/`；之后使用新报告目录重新完整执行。mypy通过是默认模式项目检查，不等于所有未标注函数的strict类型证明。唯一Celery导入边界的本地包证据见`celery_typing_boundary.json`。

## 独立安装与离线重现

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints-python312-linux.txt '.[dev,production]' pytest-cov
npm ci --prefix coding-agent --ignore-scripts --no-audit --no-fund
python scripts/run_tests.py --coverage --label final_offline
python scripts/demo_offline.py
python -m ruff check src scripts tests alembic deploy
python -m ruff format --check src scripts tests alembic deploy
python -m compileall -q src scripts tests alembic deploy
node --check coding-agent/runner.mjs
python -m pip check
python -m mypy --no-incremental src/careerforge
python -m pip install -c constraints-python312-linux.txt setuptools wheel
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
python scripts/verify_wheel.py
```

`run_tests.py`默认禁用无关pytest插件、清除供应商配置并排除production/live/sandbox标记。Pi测试使用真实SDK但合成推理；Tree-sitter测试使用实际安装语法库。无SDK/语法库的环境会明确skip，因此不能直接沿用本轮“0 skipped”结论。

## 真实基础设施重现

首选有可用Docker时运行`python scripts/verify_docker.py --confirm-docker-test`，这是独立可丢弃测试栈，不读取生产`.env`，不挂载Docker socket到应用/模型工具。

若已有明确授权的**全新可丢弃**PG/Redis：配置`CAREERFORGE_DATABASE_URL`（数据库名必须为`careerforge_v15_validation_*`）与彼此隔离的两个Redis URL，再运行：

```bash
python scripts/verify_production.py --confirm-infrastructure-test
```

该脚本拒绝非空库，迁移并测试后会保留验证用库内容，调用者应仅清理自己创建的资源。单独执行pytest生产回归时，必须明确开启：

```bash
export CAREERFORGE_TEST_POSTGRES=1
export CAREERFORGE_NATIVE_KNOWLEDGE_TEST=1
export CAREERFORGE_TEST_DATABASE_URL="$CAREERFORGE_DATABASE_URL"
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -m production \
  tests/test_runtime_postgresql.py tests/test_knowledge_postgres_integration.py \
  tests/test_cache_resilience.py
```

这轮dot环境没有可用Docker，使用`validation/current/native-infrastructure/native_harness.py`组织实际原生服务。其本轮工具路径是工作空间专用，不把未打包的系统二进制假装成可移植安装器；官方来源、签名/哈希、服务配置、命令与正常清理结果均在同目录。最终组合标签为`release-final-postgresql-redis7`和`release-final-outbox-celery`；Redis8也有独立通过记录。

## 不成立的结论

没有证明Docker资源/进程/网络隔离、真实模型/语义向量/重排质量、真实GitHub研究效果、独立人工Gold与教学收益、生产性能/持久化/灾备或全规格完成。原验证记录在`history/VALIDATION_20261029.md`；本轮没有改写原规格以降低验收标准。
