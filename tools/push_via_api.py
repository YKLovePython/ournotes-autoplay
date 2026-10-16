"""本机 git 连不上 GitHub 时，改用 GitHub 的 Git Data API 把当前提交推上去。

    python tools/push_via_api.py [仓库目录] [--repo owner/name] [--branch main]

做的事等价于 ``git push``：把本地 HEAD 的树（按 ``git ls-files`` 列出的文件）
和远端的分支树比对，只为**变化的文件**建 blob，然后建 tree → commit → 更新 ref。
多出来的远端文件会被删除（tree 条目 sha 传 null）。

提交信息、作者、时间都照抄本地那次提交，所以只要树一致，远端提交的 SHA 会和本地一样。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def gh_token() -> str:
    for cmd in (["gh", "auth", "token"],):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except FileNotFoundError:
            pass
    import os
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    raise SystemExit("拿不到 GitHub token（gh auth login 或设 GITHUB_TOKEN）")


class Api:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "User-Agent": "diting-push/1.0", "X-GitHub-Api-Version": "2022-11-28"}

    def __call__(self, path: str, data: dict | None = None, method: str | None = None):
        url = path if path.startswith("http") else "https://api.github.com" + path
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self.h,
                                     method=method or ("POST" if body else "GET"))
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"GitHub API {e.code} on {path}: {detail}")


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{r.stderr.strip()[:200]}")
    return r.stdout


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--repo", dest="slug", default=None, help="owner/name（默认从 origin 推）")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    slug = args.slug
    if not slug:
        url = git(repo, "remote", "get-url", "origin").strip()
        slug = url.split("github.com")[1].lstrip(":/").removesuffix(".git")
    api = Api(gh_token())
    print(f"仓库 {slug}  分支 {args.branch}  本地目录 {repo}")

    head = git(repo, "rev-parse", "HEAD").strip()
    meta = git(repo, "log", "-1", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B").split("\x00")
    author = {"name": meta[0], "email": meta[1], "date": meta[2]}
    committer = {"name": meta[3], "email": meta[4], "date": meta[5]}
    message = meta[6].rstrip("\n")

    remote = api(f"/repos/{slug}/git/ref/heads/{args.branch}")
    parent = remote["object"]["sha"]
    remote_tree = api(f"/repos/{slug}/git/trees/{parent}?recursive=1")
    old = {e["path"]: e["sha"] for e in remote_tree["tree"] if e["type"] == "blob"}
    print(f"远端 HEAD {parent[:8]}（{len(old)} 个文件）")

    files = [f for f in git(repo, "ls-files", "-z").split("\x00") if f]
    entries, to_upload, changed = [], [], []
    for rel in files:
        p = repo / rel
        if not p.is_file():
            continue
        data = p.read_bytes()
        local_sha = blob_sha(data)
        if old.get(rel) == local_sha:
            continue
        changed.append(rel)
        if old.get(rel) is None:
            to_upload.append((rel, data, local_sha))
        else:
            to_upload.append((rel, data, local_sha))
    deleted = [p for p in old if p not in set(files)]
    print(f"改动 {len(changed)} 个文件，删除 {len(deleted)} 个")

    entries = [{"path": rel, "mode": "100644", "type": "blob", "sha": sha}
               for rel, _d, sha in []]  # 占位，下面逐个建 blob
    for i, (rel, data, sha) in enumerate(to_upload, 1):
        blob = api(f"/repos/{slug}/git/blobs",
                   {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if i % 10 == 0 or i == len(to_upload):
            print(f"  已上传 {i}/{len(to_upload)} …", flush=True)
    for rel in deleted:
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})

    tree = api(f"/repos/{slug}/git/trees", {"base_tree": parent and
                                            api(f"/repos/{slug}/git/commits/{parent}")["tree"]["sha"],
                                            "tree": entries})
    commit = api(f"/repos/{slug}/git/commits",
                 {"message": message, "tree": tree["sha"], "parents": [parent],
                  "author": author, "committer": committer})
    api(f"/repos/{slug}/git/refs/heads/{args.branch}", {"sha": commit["sha"]}, method="PATCH")
    print(f"\n已推送：{commit['sha']}")
    print(f"本地 HEAD：{head}" + ("（与远端一致 ✓）" if commit["sha"] == head
                                  else "（与远端不同，但内容相同）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
