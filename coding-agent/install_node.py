"""Install a pinned official Node distribution in the Linux Worker build stage."""

import hashlib
import io
import platform
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "24.12.0"
DISTRIBUTIONS = {
    "x86_64": ("x64", "bdebee276e58d0ef5448f3d5ac12c67daa963dd5e0a9bb621a53d1cefbc852fd"),
    "aarch64": ("arm64", "a06d42807fb500f7459e5f3fa6cb431447352826ee6f07e14adfeec58a1b3210"),
}
architecture, expected = DISTRIBUTIONS[platform.machine()]
name = f"node-v{VERSION}-linux-{architecture}"
with urlopen(f"https://nodejs.org/dist/v{VERSION}/{name}.tar.xz", timeout=60) as response:
    data = response.read(100_000_001)
if len(data) > 100_000_000 or hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit("Official Node archive checksum mismatch")
with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
    archive.extractall("/opt", filter="data")
Path(f"/opt/{name}").rename("/opt/node")
