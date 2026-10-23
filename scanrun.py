"""One scan at a time, off the request thread, with status the page can poll.

``start()`` is the only way the dashboard scans: it serializes runs behind a lock,
so a rescan click during the startup scan reports "already running" instead of
racing it on the DB.
"""

import threading
import time

import sources

_lock = threading.Lock()
projects_dir = None  # the dashboard's --projects-dir override; status and rescans honor it
_state = {"state": "idle", "started": None, "finished": None, "result": None, "error": None}


def status():
    p = sources.PROGRESS
    return dict(_state, source=p["source"], done=p["done"], total=p["total"],
                file_errors=list(p["errors"]))


def _run(db_path, projects_dir, out):
    try:
        per_source = sources.scan_all(db_path=db_path, projects_dir=projects_dir, verbose=False)
        _state["result"] = {k: sum(s[k] for s in per_source.values())
                            for k in ("new", "updated", "skipped", "turns", "sessions")}
        _state["result"]["sources"] = per_source
        _state["state"] = "done"
    except Exception as e:  # surfaced on the page; the server stays up
        _state.update(state="error", error=f"{type(e).__name__}: {e}")
    finally:
        _state["finished"] = time.time()
        out.append(status())  # this run's own outcome, taken before another run can reset the state
        _lock.release()


def start(db_path, root=None, wait=False):
    """Start a scan unless one is running. Returns None if one already was; otherwise True,
    or with ``wait`` the finished run's own status."""
    global projects_dir
    if not _lock.acquire(blocking=False):
        return None
    projects_dir = root or projects_dir
    sources.PROGRESS.update(source=None, done=0, total=0, errors=[])
    _state.update(state="running", started=time.time(), finished=None, result=None, error=None)
    out = []
    t = threading.Thread(target=_run, args=(db_path, projects_dir, out), daemon=True)
    t.start()
    if wait:
        t.join()
        return out[0]
    return True
