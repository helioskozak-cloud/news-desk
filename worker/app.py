"""
Render free-tier worker that runs the news-desk refresh on /refresh.

Architecture: rather than running a background daemon thread (which
proved unreliable under Render's gunicorn setup), we expose a /refresh
endpoint that synchronously runs one fetch + commit + push cycle.
An external pinger (cron-job.org, GitHub Actions, UptimeRobot) hits
/refresh on whatever schedule we want.

/refresh is idempotent and self-locking — concurrent calls return early.
"""
import os
import sys
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

print("[worker] module loading", flush=True)

REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/render/project/src"))
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_OWNER = os.environ.get("GH_OWNER", "helioskozak-cloud")
REPO_NAME = os.environ.get("REPO_NAME", "news-desk")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "").strip()

print(f"[worker] REPO_DIR={REPO_DIR} exists={REPO_DIR.exists()} "
      f"has_git={(REPO_DIR / '.git').exists()} pat_len={len(GH_PAT)}", flush=True)

app = Flask(__name__)
_lock = threading.Lock()
_git_configured = False

_status = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_run_at": None,
    "last_run_result": None,
    "last_commit_at": None,
    "last_error": None,
    "runs": 0,
    "commits": 0,
}


def _run(cmd, timeout=180):
    return subprocess.run(
        cmd,
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _configure_git():
    global _git_configured
    if _git_configured:
        return
    if not GH_PAT:
        raise RuntimeError("GH_PAT env var not set")
    for cmd in (
        ["git", "config", "user.name", "news-desk-render-worker"],
        ["git", "config", "user.email", "worker@news-desk"],
        ["git", "config", "--global", "--add", "safe.directory", str(REPO_DIR)],
    ):
        r = _run(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {r.stderr[-300:]}")
    auth_url = f"https://x-access-token:{GH_PAT}@github.com/{GH_OWNER}/{REPO_NAME}.git"
    r = _run(["git", "remote", "set-url", "origin", auth_url])
    if r.returncode != 0:
        r = _run(["git", "remote", "add", "origin", auth_url])
        if r.returncode != 0:
            raise RuntimeError(f"remote add origin failed: {r.stderr[-300:]}")
    _run(["git", "fetch", "--unshallow", "origin"])
    _run(["git", "branch", "--set-upstream-to=origin/main", "main"])
    _git_configured = True
    print("[worker] git configured", flush=True)


def _refresh_once():
    _configure_git()

    pull = _run(["git", "pull", "--rebase", "--autostash", "origin", "main"])
    if pull.returncode != 0:
        raise RuntimeError(f"pull failed: {pull.stderr[-400:]}")

    news = _run([sys.executable, "scan/fetch_news.py"], timeout=300)
    if news.returncode != 0:
        raise RuntimeError(f"fetch_news failed: {news.stderr[-400:]}")

    _run([sys.executable, "scan/fetch_stocks.py"], timeout=300)

    _run(["git", "add", "docs/data/headlines.json", "docs/data/stocks.json"])
    diff = _run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return False

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    commit = _run(["git", "commit", "-m", f"chore: refresh feed {stamp}"])
    if commit.returncode != 0:
        raise RuntimeError(f"commit failed: {commit.stderr[-400:]}")

    push = _run(["git", "push", "origin", "HEAD:main"])
    if push.returncode != 0:
        _run(["git", "pull", "--rebase", "origin", "main"])
        push = _run(["git", "push", "origin", "HEAD:main"])
        if push.returncode != 0:
            raise RuntimeError(f"push failed: {push.stderr[-400:]}")
    return True


@app.route("/")
@app.route("/health")
def health():
    return jsonify({"ok": True, "now": datetime.now(timezone.utc).isoformat(), **_status}), 200


@app.route("/refresh", methods=["GET", "POST"])
def refresh():
    if REFRESH_TOKEN:
        tok = request.args.get("token") or request.headers.get("X-Refresh-Token", "")
        if tok != REFRESH_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    if not _lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "refresh already running"}), 409

    start = datetime.now(timezone.utc)
    try:
        _status["last_run_at"] = start.isoformat()
        _status["runs"] += 1
        committed = _refresh_once()
        _status["last_run_result"] = "committed" if committed else "no-changes"
        if committed:
            _status["last_commit_at"] = start.isoformat()
            _status["commits"] += 1
        _status["last_error"] = None
        print(f"[{start.isoformat()}] {_status['last_run_result']}", flush=True)
        return jsonify({"ok": True, "result": _status["last_run_result"], **_status}), 200
    except Exception as e:
        _status["last_run_result"] = "error"
        _status["last_error"] = str(e)[:400]
        print(f"[{start.isoformat()}] error: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e), **_status}), 500
    finally:
        _lock.release()


print("[worker] module loaded", flush=True)
