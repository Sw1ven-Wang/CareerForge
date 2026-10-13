"""One command per container lifetime; network disabled by Compose."""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

MAIL = Path("/mailbox")
ROOT = Path("/workspace")


def main():
    if os.getpid() != 1:
        raise RuntimeError("Run only as PID 1 in the dedicated sandbox container")
    MAIL.mkdir(exist_ok=True)
    MAIL.chmod(0o700)
    for name in ("ready", "job.json"):
        (MAIL / name).unlink(missing_ok=True)
    (MAIL / "ready").write_text(str(uuid4()))
    while not (MAIL / "job.json").exists():
        time.sleep(0.2)
    job = json.loads((MAIL / "job.json").read_text())
    (MAIL / "ready").unlink(missing_ok=True)
    for name, text in job["files"].items():
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(0o666)
    ROOT.chmod(0o777)
    for directory, _, _ in os.walk(ROOT):
        Path(directory).chmod(0o777)

    def drop():
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)

    def stop_children():
        # Dedicated PID namespace: also catches detached grandchildren.
        try:
            os.kill(-1, signal.SIGSTOP)
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                job["argv"],
                cwd=ROOT,
                stdout=stdout,
                stderr=stderr,
                preexec_fn=drop,
                env={
                    "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            try:
                exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                stop_children()
                process.wait(timeout=3)
                exit_code = 124
            stop_children()

            def tail(stream):
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 16000))
                return stream.read().decode("utf-8", "replace")

            out = {
                "exit_code": exit_code,
                "stdout": tail(stdout),
                "stderr": tail(stderr),
            }
            if exit_code == 124:
                out["stderr"] += "\nCommand timed out after 30 seconds"
        except OSError:
            out = {
                "exit_code": 127,
                "stdout": "",
                "stderr": "Executable not available in this runtime",
            }
        finally:
            stop_children()
    files = {}
    size = 0
    complete = True
    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in names:
            target = Path(directory) / name
            if target.is_symlink() or not target.is_file():
                continue
            try:
                if target.stat().st_size > 100000:
                    complete = False
                    continue
                text = target.read_text()
                size += len(text.encode())
                if size > 2_000_000 or len(files) >= 100:
                    complete = False
                    continue
                files[str(target.relative_to(ROOT))] = text
            except (UnicodeError, OSError):
                complete = False
    out.update(result_files=files, files_captured=complete, job_id=job["job_id"])
    tmp = MAIL / "result.tmp"
    tmp.write_text(json.dumps(out))
    tmp.replace(MAIL / "result.json")
    # PID 1 exit terminates every descendant; restart recreates the tmpfs.
    os._exit(0)


if __name__ == "__main__":
    main()
