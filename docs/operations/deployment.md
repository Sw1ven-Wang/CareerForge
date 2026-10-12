# 本机私有部署与恢复

## 运行边界

V1 离线预览，回环入口 `127.0.0.1:8080`；API、Linux Celery Worker、dispatcher、PostgreSQL、Redis 分进程。session cookie、CSRF、Origin、owner 与完整版本校验均由 API 执行。样例 Compose 不是公网 SaaS 配置；公网/TLS 与外部模型均未启用。

标准 Nginx 镜像构建因 Docker Hub token 连接超时失败。已构建并启动 `compose.offline.yaml` 路径：本机 npm 生成 dist，Python 本地镜像提供静态文件及流式 API 代理。它不依赖下载 Node/Nginx 基础镜像，但仍需先具备 backend 镜像与锁定依赖缓存。

启动命令见根 README。API 健康检查：`GET http://127.0.0.1:8080/health/ready`。修改前端后重新运行 npm build，再 build frontend 并重建该容器；修改服务端后重建 API/worker/dispatcher/migrate。

## 备份

```powershell
uv run --project backend python scripts/backup_database.py --database careerforge_test --output private/backups/manual-new-name.dump
```

使用 pg_dump 自定义二进制格式，Python 写字节，避免 PowerShell 重定向破坏二进制；现有输出文件不覆盖。备份包含私有材料和账号散列，只存入 private，不放入求职证据包。

## 恢复到独立新库

```powershell
uv run --project backend python scripts/restore_database.py private/backups/manual-new-name.dump --database careerforge_restore_manual_new
uv run --project backend python scripts/compare_database.py careerforge_test careerforge_restore_manual_new
```

createdb 在同名库已存在时失败；不执行 DROP、清空或覆盖。校验只输出表/行数量与是否一致，不输出材料正文或密码散列。切换服务到恢复库应在停止业务写入并核对数据后由部署者明确配置，工具不自动切换。

实际验证：从 `careerforge_v1_validation_20261012` 备份到 private，再恢复为 `careerforge_restore_validation_20261012`；56 张表、430 行的聚合哈希一致。原开发库未被覆盖。

## 干净环境

将 CAREERFORGE_DATABASE_URL 指向一个新建隔离数据库，执行 Alembic upgrade head，然后运行不带 --queue 的 run_offline_demo.py。已在独立新库完成。测试环境必须设置 TEST_DATABASE_URL；缺少该变量时基础设施测试会跳过，不能算通过。

物理擦除测试另要求数据库名含 `careerforge_v1_validation_`。开发库不能为了测试批量重置清理游标。
