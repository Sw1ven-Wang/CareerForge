# 1.6.0 末版验证与重现（2026-11-02）

## 权威结果

**666个唯一测试全部对账：639离线 + 27真实PG/Redis，全部passed，0失败/错误/跳过/遗漏/重复。** 项目mypy：Success: no issues found in 72 source files。Ruff/格式/编译/Node/pip及wheel均通过。**没有完整覆盖率结果**：首次插桩尝试1200秒超时，缺完整JUnit/coverage；末版完整串行测试未插桩。测试数不是质量或需求覆盖率。

最后仅验证器的空analysis探针补齐冻结taxonomy合同，应用和测试代码不变；58项部署关联回归+真实worker复验通过，见`post_probe_deployment.xml`与`code_freeze.json`的delta。

权威目录 `../validation/v16/final_verified/`：`summary.json`、`validation_commands.json`、`offline_run.json`、`offline.xml`、`native_main.xml`、`native_retrieval.xml`、`native_governance.xml`、`static_checks.json`、`source_freeze_verified.json`。真实生产链路 `production/production.json`；Docker `docker/docker.json`明确blocked。

`validation/current/`仅1.5.1检查点；`validation/v16/final/`是首次1.6覆盖率超时/旧错误码断言失败尝试；各模块早期失败原样保留。修正维护来源错误码断言后已再次完整执行，不能拿旧失败当末版，也不能把中间结果叠加计数。两次全套尝试曾SIGSEGV（-11），含无覆盖率串行尝试。独立无产品代码的ORM最小程序也在异步栈转储时2/2崩溃、关闭时2/2正常；记录在`diagnostic_fault/`。已将计时栈转储变为显式opt-in，默认0，独立硬超时与所有用例仍保留。精确解释器C根因未宣称修复。末版完整串行测试使用修正后的runner，没有跳过大图或typed replay。

## 离线与静态重现

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints-python312-linux.txt '.[dev,production]' pytest-cov
npm ci --prefix coding-agent --ignore-scripts --no-audit --no-fund
python scripts/run_tests.py --output-dir validation/local/recheck
# 可选覆盖率另开目录、串行执行；本次未取得完整覆盖率结果：
# python scripts/run_tests.py --coverage --timeout 2400 --output-dir validation/local/coverage-recheck
python scripts/demo_offline.py
python -m ruff check src scripts tests alembic deploy
python -m ruff format --check src scripts tests alembic deploy
python -m compileall -q src scripts tests alembic deploy
node --check coding-agent/runner.mjs
python -m pip check
python -m mypy --no-incremental src/careerforge
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
python scripts/verify_wheel.py --output-dir validation/local/recheck-wheel
python scripts/verify_integrity.py
```

验证脚本拒绝覆写已有同名证据；用新目录/label。默认离线超时1200秒，超时不是通过。本版默认不启动异步faulthandler计时转储（--traceback-after默认0）；显式开启须自行评估已记录的运行时崩溃风险。真实1200秒父进程超时仍exit124，不把进度点当通过。卸载SDK/语法库会导致明确skip，不能沿用本次0skip结论。仅Celery外部未提供类型标记的导入有精确豁免；默认mypy不等于全面strict。

## 真实基础设施重现

首选已获授权且具备Docker daemon/cgroup的隔离环境：

```bash
python scripts/verify_docker.py --confirm-docker-test
```

若运行native测试，先准备全新可丢弃的PG17/pgvector0.8及两个隔离Redis端点（broker/cache）。配置数据库名为 `careerforge_v15_validation_<unique>` 的`CAREERFORGE_DATABASE_URL`、`CAREERFORGE_TEST_DATABASE_URL`，以及两个Redis URL。禁止指向生产库。

```bash
export CAREERFORGE_TEST_POSTGRES=1
export CAREERFORGE_NATIVE_KNOWLEDGE_TEST=1
export CAREERFORGE_NATIVE_RETRIEVAL_V16=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -m production \
  tests/test_knowledge_postgres_integration.py tests/test_cache_resilience.py \
  tests/test_runtime_postgresql.py tests/test_runtime_ext_postgresql.py \
  tests/test_privacy_postgresql_v16.py
```

**顺序与隔离重要**：knowledge文件创建测试bootstrap身份，放在主组前面。检索probe依赖没有其他模块制造的公共默认版本冲突，须改用第二个全新库：

```bash
# 先把数据库URL切换至另一全新、可丢弃验证库
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -m production   tests/test_retrieval_native_v16.py
```

治理PG测试有自己的bootstrap身份，须改用第三个全新库/集群，而不是削弱产品bootstrap权限保护：

```bash
export CAREERFORGE_NATIVE_GOVERNANCE_TEST=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -m production \
  tests/test_governance_postgres_v16.py
```

再用第四个全新空库执行 `python scripts/verify_production.py --confirm-infrastructure-test --output-dir validation/local/production-recheck`。验证器拒绝非空库，检查迁移、vector、跨客户端Redis锁、独立随机Celery队列/worker/Outbox、0模型收据。已有真实基础设施的清理由创建者负责，不能全局prune/drop。

本轮原生harness在 `validation/v16/native-infrastructure/native_harness.py`；标签 `final-v16-main-isolated`、`final-v16-retrieval`、`final-v16-governance`、`final-v16-outbox-fixed` 保存实际命令、版本、退出码、服务停止与临时数据移除。工具二进制未打包，默认路径为本轮工作空间专用；可通过`--tool-root/--redis-root`明确配置同结构的已核验工具。不能把这个路径假装成可移植安装器。官方来源/签名/SHA证据留在旧native工具准备记录。

## 验收边界

真实PG/Redis/Celery通过不等于Docker镜像/资源/进程/网络隔离通过；本次Dockerblocked，构建/容器not_run。实际Pi/解析器不等于真实模型效果。Gold、学习收益、语义召回、真实GitHub研究、生产负载/持久性/灾备、完整原工程兼容均没有被工程测试证明。详见`KNOWN_GAPS.md`。
