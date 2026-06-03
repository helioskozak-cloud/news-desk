"""
Render free-tier worker. On-demand async /refresh endpoint.

POST /refresh kicks off a background refresh and returns 202 immediately
so Render's edge gateway doesn't 502 us on long fetches. GET /refresh
and /health return current status. An external pinger (cron-job.org or
similar) hits POST /refresh on whatever schedule we want.
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
    "in_flight": False,
}


def _run(cmd, timeout=120):
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
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {r.stderr[-200:]}")
    auth_url = f"https://x-access-token:{GH_PAT}@github.com/{GH_OWNER}/{REPO_NAME}.git"
    r = _run(["git", "remote", "set-url", "origin", auth_url])
    if r.returncode != 0:
        r = _run(["git", "remote", "add", "origin", auth_url])
        if r.returncode != 0:
            raise RuntimeError(f"remote add origin failed: {r.stderr[-200:]}")
    _git_configured = True
    print("[worker] git configured", flush=True)


def _refresh_once():
    _configure_git()

    fetch = _run(["git", "fetch", "--depth", "1", "origin", "main"])
    if fetch.returncode != 0:
        raise RuntimeError(f"fetch failed: {fetch.stderr[-300:]}")
    reset = _run(["git", "reset", "--hard", "origin/main"])
    if reset.returncode != 0:
        raise RuntimeError(f"reset failed: {reset.stderr[-300:]}")

    news = _run([sys.executable, "scan/fetch_news.py"], timeout=180)
    if news.returncode != 0:
        raise RuntimeError(f"fetch_news failed: {news.stderr[-300:]}")

    # Skip fetch_stocks.py — yfinance+pandas OOMs Render free tier (512MB).
    # GitHub Actions cron still runs both scripts at lower frequency.

    _run(["git", "add", "docs/data/headlines.json"])
    diff = _run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return False

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    commit = _run(["git", "commit", "-m", f"chore: refresh feed {stamp}"])
    if commit.returncode != 0:
        raise RuntimeError(f"commit failed: {commit.stderr[-300:]}")

    push = _run(["git", "push", "origin", "HEAD:main"])
    if push.returncode != 0:
        raise RuntimeError(f"push failed: {push.stderr[-300:]}")
    return True


def _do_refresh():
    start = datetime.now(timezone.utc)
    _status["last_run_at"] = start.isoformat()
    _status["runs"] += 1
    try:
        committed = _refresh_once()
        _status["last_run_result"] = "committed" if committed else "no-changes"
        if committed:
            _status["last_commit_at"] = start.isoformat()
            _status["commits"] += 1
        _status["last_error"] = None
        print(f"[{start.isoformat()}] {_status['last_run_result']}", flush=True)
    except Exception as e:
        _status["last_run_result"] = "error"
        _status["last_error"] = str(e)[:400]
        print(f"[{start.isoformat()}] error: {e}", flush=True)
    finally:
        _status["in_flight"] = False
        _lock.release()


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

    if request.method == "GET":
        return jsonify({"ok": True, "now": datetime.now(timezone.utc).isoformat(), **_status}), 200

    if not _lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "refresh already in flight", **_status}), 409

    _status["in_flight"] = True
    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"ok": True, "queued": True, **_status}), 202


print("[worker] module loaded", flush=True)
