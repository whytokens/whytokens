"""version.py - which build is running: {version, commit, started_at}. Stdlib only, no network."""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_STARTED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _version(root):
    try:  # a checkout's pyproject.toml beats installed metadata, which goes stale under an editable install
        for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version("whytokens")
    except Exception:
        return None


def _commit(root):
    if not (root / ".git").exists():
        return None
    try:
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True,
                                        text=True, timeout=5, check=True).stdout.strip()
        sha = run("rev-parse", "--short", "HEAD")
        return sha + ("-dirty" if run("status", "--porcelain", "--untracked-files=no") else "")
    except Exception:
        return None


def build_info(root=_HERE):
    return {"version": _version(root), "commit": _commit(root), "started_at": _STARTED}


INFO = build_info()  # read once at process start


def line():
    return " ".join(x for x in ("whytokens", INFO["version"], INFO["commit"]) if x)
