# 回归执行保障修复

本修复处理执行与证据保存，不改变 CareerForge 的业务审核、知识保留、预算、租约或测试期限。历史 RC11 运行会话无法恢复的根因仍未知；日志停在约 65% 不能证明程序在该位置失败或已经退出。

## 单次运行

```sh
python -m pip install -c constraints-python312-linux.txt '.[dev,production]' pytest-cov
python scripts/run_tests.py --workers 4 --timeout 2400 \
  --output-dir validation/new-full --label full
python scripts/run_tests.py --status --output-dir validation/new-full --label full
```

新的 `--workers` 显式控制并行度。继承的 `PYTEST_ADDOPTS` 和 `PYTEST_PLUGINS` 被清除；它们不能把本次运行变成只收集、提前停止或加载未知插件。原有 production、live、sandbox 排除表达式不变。

输出包含：

- 日志及 JUnit XML；进程运行时直接写文件，不依赖终端读者继续连接。
- 原子替换并 fsync 的状态文件、周期心跳、启动身份（PID、启动时钟、boot ID、PID namespace）。
- 控制器和每个 worker 的逐事件 JSONL。每条记录写后 fsync，包含实际节点、setup/call/teardown、结果和会话结束事件。
- 测试前后源文件哈希、依赖版本、最终节点清单与证据哈希。终态在全部证据持久化之后最后提交。

通过必须同时满足 pytest 退出为 0、完整且一致的收集和 worker 生命周期、每个已选节点恰好完整执行、有效 JUnit、源码未变化和进程组清理已确认。存在 pytest 断言失败与缺少可靠执行证据是不同状态。

SIGINT/SIGTERM 与超时会对本次创建的进程组执行有界 TERM/KILL 清理。SIGHUP 被记录但不会放弃测试。SIGKILL、整台主机终止、文件系统故障不能可靠运行清理代码，因此不能保证写出终态。此时只保留可确认的已完成节点及未知结果，不伪造退出码，不自动重试。

全项目 OS 锁和持久 active-run 标记会阻止重复启动。锁被释放不等于未结案标记可以自动删除。若上次被强制终止，先只读检查状态、源身份和该主机上的实际进程/资源；确认没有遗留执行者后，才由操作者处理旧标记。不要通过换 label 或复制目录绕过仍存疑的活动运行。新机制无法追溯约束启用它之前的旧进程。

## 短批次及完整节点覆盖

```sh
python scripts/run_regression_batches.py plan \
  --output-dir validation/new-batched --workers 4 --timeout 2400 \
  --batch-size 450 --batch-timeout 600
python scripts/run_regression_batches.py next --output-dir validation/new-batched
python scripts/run_regression_batches.py status --output-dir validation/new-batched
```

重复执行 `next`，每次最多启动一个新批次。计划先完成一次全收集，固定源文件指纹、完整已选/排除节点列表和每批参数。运行开始后使用同一 boot ID 下的单调时钟记录总截止时间；批次之间的等待也计入原总时限。每批的期限不会超过剩余总预算。

已完成批次的哈希、终态和节点都要重新核对。被 claim 但没有完整终态的批次不会重跑；源码变化、证据损坏或期限耗尽都会停止后续运行。最终必须对照固定全集证明全部节点只执行一次。这里的“恢复”是读取持久证据并推进**尚未启动**的批次，不是重试结果未知的旧测试。

分批是多个独立 pytest 会话。即使覆盖了全部离线节点，也不能声称复现了历史单会话并行拓扑、生产基础设施、原生模型语义收益或完整业务闭环。报告应分别给出这些边界。

## 执行器控制测试

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin \
  -q scripts/tests
```

这些控制使用临时 mini-project 与合成用例，验证断开观察端、信号、超时、并发锁、来源变化及证据损坏等情况。它们不计入 CareerForge 原有离线业务节点数量，也不等于真实模型调用。

## 使用限制

当前实现使用 POSIX process group、flock 和 Linux `/proc` 身份检查，适用于当前 Linux 验证环境，不承诺 Windows 兼容性。仅可约束自身创建且没有主动脱离进程组的测试子进程。记录心跳不代表业务正在取得进展。文件系统消失时，只有已经另外保存的交付物可以恢复；本地 fsync 不等于异地备份。
