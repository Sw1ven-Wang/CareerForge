"""Live Docker isolation smoke tests. Explicit confirmation and immutable image required."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PROBE = """import json, os, pathlib, socket, sys
assert os.getuid() == 65534 and os.getgid() == 65534
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status
assert 'Seccomp:\\t2' in status
assert int(next(x.split()[1] for x in status.splitlines() if x.startswith('CapEff:')), 16) == 0
try:
    pathlib.Path('/rootfs-must-be-readonly').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
try:
    pathlib.Path('/input/job.json').write_text('{}')
except OSError:
    pass
else:
    raise AssertionError('snapshot input writable')
interfaces = {line.split(':')[0].strip() for line in pathlib.Path('/proc/net/dev').read_text().splitlines()[2:]}
assert interfaces == {'lo'}, interfaces
sock = socket.socket(); sock.settimeout(.5)
try:
    sock.connect(('192.0.2.1', 9))
except OSError:
    pass
else:
    raise AssertionError('network isolation failed')
finally:
    sock.close()
cgroup = pathlib.Path('/sys/fs/cgroup')
assert (cgroup / 'memory.max').read_text().strip() == '268435456'
assert (cgroup / 'pids.max').read_text().strip() == '64'
quota, period = (cgroup / 'cpu.max').read_text().split()
assert quota != 'max' and int(quota) <= int(period)
pathlib.Path('result.txt').write_text('sandbox-ok')
private = pathlib.Path('private-dir'); private.mkdir(mode=0o700)
(private / 'private.txt').write_text('private-output-ok')
(private / 'private.txt').chmod(0o600)
pathlib.Path('.env').write_text('must-not-be-exported')
os.symlink('/etc/passwd', 'passwd-link')
print('isolation-ok')
print('stderr-ok', file=sys.stderr)
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-docker-test", action="store_true")
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "validation" / "current" / ("sandbox-" + uuid4().hex[:12]),
    )
    options = parser.parse_args(argv)
    output = options.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "sandbox.json"
    if target.exists():
        parser.error("refusing to overwrite sandbox.json")
    report = {
        "status": "blocked",
        "scope": "real_docker_isolation_smoke_not_security_certification",
        "image_digest": options.image,
        "started_at": datetime.now(UTC).isoformat(),
        "checks": {k: "not_run" for k in ("isolation_and_export", "nonzero_exit", "deadline")},
    }
    stage = None
    try:
        if not options.confirm_docker_test:
            report["reason_code"] = "explicit_docker_test_authorization_required"
            return_code = 2
        else:
            from careerforge.adapters.sandbox import DockerSandbox

            runner = DockerSandbox()
            stage = "isolation_and_export"
            receipt = runner.execute(
                {"image_digest": options.image, "files": {"probe.py": PROBE}},
                ["python", "-B", "probe.py"],
            )
            if (
                receipt["exit_code"] != 0
                or receipt["timed_out"]
                or receipt["files"].get("result.txt") != "sandbox-ok"
                or receipt["files"].get("private-dir/private.txt") != "private-output-ok"
                or "passwd-link" in receipt["files"]
                or ".env" in receipt["files"]
                or "isolation-ok" not in receipt["stdout"]
                or "stderr-ok" not in receipt["stderr"]
            ):
                raise RuntimeError("isolation_or_export_mismatch")
            report["checks"][stage] = "passed"
            stage = "nonzero_exit"
            receipt = runner.execute(
                {"image_digest": options.image, "files": {}},
                ["python", "-c", "raise SystemExit(7)"],
            )
            if receipt["exit_code"] != 7 or receipt["timed_out"]:
                raise RuntimeError("exit_code_mismatch")
            report["checks"][stage] = "passed"
            stage = "deadline"
            receipt = runner.execute(
                {"image_digest": options.image, "files": {}},
                ["python", "-c", "import time; time.sleep(90)"],
            )
            if receipt["exit_code"] != 124 or receipt["timed_out"] is not True:
                raise RuntimeError("timeout_mismatch")
            report["checks"][stage] = "passed"
            report["status"] = "passed_docker_sandbox_smoke_only"
            return_code = 0
    except Exception as exc:
        report.update(
            status="failed",
            error_type=type(exc).__name__,
            reason_code=getattr(exc, "code", "sandbox_check_failed"),
        )
        if stage:
            report["checks"][stage] = "failed"
        return_code = 2
    report["finished_at"] = datetime.now(UTC).isoformat()
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
