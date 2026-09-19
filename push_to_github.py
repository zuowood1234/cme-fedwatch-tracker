#!/usr/bin/env python3
"""
Push data files to GitHub via the Contents API.
Used by the daily automation and can be run manually.
"""
import argparse
import base64
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from serverchan import push_daily_summary


def push_file(repo: str, token: str, git_path: str, local_path: str) -> bool:
    """Push a single file to GitHub. Returns True on success."""
    api_base = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    # Get existing file SHA for update
    sha = None
    resp = requests.get(f"{api_base}/contents/{git_path}", headers=headers, timeout=15)
    if resp.status_code == 200:
        sha = resp.json().get("sha")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "message": f"Daily CME FedWatch update ({today})",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(
        f"{api_base}/contents/{git_path}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"Failed to push {git_path}: {resp.status_code} {resp.text[:200]}")
        return False
    print(f"Pushed {git_path}")
    return True


def git_push_data(data_dir: Path, today_str: str) -> bool:
    """Fallback: push data files via git (uses locally configured git credentials)."""
    try:
        repo_root = Path(__file__).resolve().parent
        cmds = [
            ["git", "-C", str(repo_root), "add", "data/"],
            ["git", "-C", str(repo_root), "commit", "-m",
             f"Daily CME FedWatch update ({today_str}) [git fallback]"],
            ["git", "-C", str(repo_root), "push"],
        ]
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                print(f"git step failed: {' '.join(c)}\n{r.stderr.strip()}")
                return False
        print("Pushed data via git.")
        return True
    except Exception as e:
        print(f"git fallback error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Push CME FedWatch data to GitHub")
    parser.add_argument("--repo", default="zuowood1234/cme-fedwatch-tracker", help="GitHub repo slug")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub PAT (optional)")
    parser.add_argument("--data-dir", default="./data", help="Local data directory")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    files_to_push = []
    history_csv = data_dir / "fedwatch_history.csv"
    if history_csv.exists():
        files_to_push.append(("data/fedwatch_history.csv", history_csv))
    daily_json = data_dir / "daily" / f"{today_str}.json"
    if daily_json.exists():
        files_to_push.append((f"data/daily/{today_str}.json", daily_json))

    if not files_to_push:
        print("No data files to push.")

    # Try GitHub Contents API first (needs a valid PAT)...
    ok = True
    if args.token:
        for git_path, local_path in files_to_push:
            if not push_file(args.repo, args.token, git_path, local_path):
                ok = False
    else:
        ok = False

    # ...fall back to git push if the API failed (e.g. expired PAT).
    if not ok:
        print("GitHub API push failed/unavailable; falling back to git push...")
        ok = git_push_data(data_dir, today_str)

    # Send WeChat summary via ServerChan — independent of GitHub push result,
    # so a GitHub failure (e.g. expired PAT) does NOT block the WeChat alert.
    if os.environ.get("SERVERCHAN_SENDKEY"):
        print("Sending ServerChan daily summary...")
        sc_ok, sc_msg = push_daily_summary(args.data_dir)
        if sc_ok:
            print("ServerChan summary sent.")
        else:
            print(f"ServerChan summary failed: {sc_msg}")
    else:
        print("SERVERCHAN_SENDKEY not set; skipping WeChat push.")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
