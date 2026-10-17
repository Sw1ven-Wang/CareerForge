"""API talks to a networkless disposable runner through a private mailbox."""

import json
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.session import get_engine


def run(files: dict[str, str], argv: list[str]) -> Json:
    mail = Path("/mailbox")
    if not mail.is_dir():
        raise BusinessError(
            503, "SANDBOX_UNAVAILABLE", "请启动 compose.ide.yaml 中的隔离执行服务。"
        )
    # Serialize all mailbox access, including across API processes.
    with Session(get_engine()) as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(8315713)")):
            raise BusinessError(409, "SANDBOX_BUSY", "隔离执行环境忙，请稍后重试。")
        try:
            ready = mail / "ready"
            if not ready.exists():
                raise BusinessError(503, "SANDBOX_UNAVAILABLE", "隔离环境正在重启或不可用。")
            token = ready.read_text()
            job_id = str(uuid4())
            (mail / "result.json").unlink(missing_ok=True)
            tmp = mail / "job.tmp"
            tmp.write_text(json.dumps({"job_id": job_id, "files": files, "argv": argv}))
            tmp.replace(mail / "job.json")
            deadline = time.monotonic() + 40
            result: Json | None = None
            while time.monotonic() < deadline:
                target = mail / "result.json"
                if target.exists():
                    data = json.loads(target.read_text())
                    if data["job_id"] == job_id:
                        result = data
                if result and ready.exists() and ready.read_text() != token:
                    return result
                time.sleep(0.2)
            raise BusinessError(
                504, "COMMAND_TIMEOUT", "命令超时，未自动重试。请检查隔离执行服务。"
            )
        except OSError:
            raise BusinessError(503, "SANDBOX_UNAVAILABLE", "隔离服务通信失败。") from None
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(8315713)"))
            lock.commit()
