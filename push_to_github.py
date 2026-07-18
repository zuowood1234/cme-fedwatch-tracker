#!/usr/bin/env python3
"""
Push data files to GitHub via the Contents API.
Used by the daily automation and can be run manually.
"""
import argparse
import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


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


def main():
    parser = argparse.ArgumentParser(description="Push CME FedWatch data to GitHub")
    parser.add_argument("--repo", default="zuowood1234/cme-fedwatch-tracker", help="GitHub repo slug")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub PAT")
    parser.add_argument("--data-dir", default="./data", help="Local data directory")
    args = parser.parse_args()

    if not args.token:
        print("ERROR: GITHUB_TOKEN not provided")
        sys.exit(1)

    data_dir = Path(args.data_dir)
    files_to_push = []

    history_csv = data_dir / "fedwatch_history.csv"
    if history_csv.exists():
        files_to_push.append(("data/fedwatch_history.csv", history_csv))

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_json = data_dir / "daily" / f"{today_str}.json"
    if daily_json.exists():
        files_to_push.append((f"data/daily/{today_str}.json", daily_json))

    if not files_to_push:
        print("No data files to push.")
        sys.exit(0)

    ok = True
    for git_path, local_path in files_to_push:
        if not push_file(args.repo, args.token, git_path, local_path):
            ok = False

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
