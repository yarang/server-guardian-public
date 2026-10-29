"""
Installs the official Go release in ~/.local/go on the hub after checking go.dev's published sha256
(deploy/hub/install.sh; the toolchain checks and builds judge patches to Go files, patch_ops).

    GO_VERSION=1.26.5 GO_ARCH=arm64 python3 install_go.py
"""
import hashlib, json, os, shutil, subprocess, tarfile, tempfile, urllib.request
from pathlib import Path

version, arch = "go" + os.environ["GO_VERSION"], os.environ["GO_ARCH"]
home = Path.home()
root, bindir = home / ".local" / "go", home / ".local" / "bin"
current = root / "bin" / "go"
if current.exists() and subprocess.run([str(current), "version"], capture_output=True, text=True).stdout.split()[2:3] == [version]:
    print(f"  {version} already installed")
    raise SystemExit(0)
with urllib.request.urlopen("https://go.dev/dl/?mode=json&include=all", timeout=60) as resp:
    releases = json.load(resp)
name = f"{version}.linux-{arch}.tar.gz"
entry = next((f for r in releases for f in r["files"] if f["filename"] == name), None)
if entry is None:
    raise SystemExit(f"{name} is not listed on go.dev")
with tempfile.TemporaryDirectory(dir=home / ".local") as tmp:
    archive = Path(tmp) / name
    with urllib.request.urlopen(f"https://go.dev/dl/{name}", timeout=600) as resp, open(archive, "wb") as out:
        shutil.copyfileobj(resp, out)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        raise SystemExit(f"sha256 mismatch for {name}: {digest} != {entry['sha256']}")
    with tarfile.open(archive) as tar:
        tar.extractall(tmp, filter="data")
    old = home / ".local" / "go.old"
    shutil.rmtree(old, ignore_errors=True)
    if root.exists():
        root.rename(old)
    (Path(tmp) / "go").rename(root)
    shutil.rmtree(old, ignore_errors=True)
bindir.mkdir(parents=True, exist_ok=True)
for tool in ("go", "gofmt"):
    link = bindir / tool
    if link.is_symlink() or not link.exists():
        link.unlink(missing_ok=True)
        link.symlink_to(root / "bin" / tool)
print("  " + subprocess.run([str(current), "version"], capture_output=True, text=True).stdout.strip())
