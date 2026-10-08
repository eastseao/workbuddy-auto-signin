#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地令牌导出 · 注入 GitHub 仓库 Secret
======================================

作用（只在**本机**跑，云端 runner 不跑这个）：
  1) 读取本机 WorkBuddy 客户端登录态，解密出**长效令牌 refresh token**；
  2) 通过 `gh secret set` 把令牌注入目标仓库的 Secret（默认 WB_REFRESH_TOKEN）；
  3) 顺带可注入飞书 webhook（FEISHU_WEBHOOK / FEISHU_SECRET）。

安全设计
--------
* 令牌**只经 stdin** 交给 `gh`，不出现在命令行参数里（避免 `ps` 泄漏）；
* 脚本自身**从不把令牌写到 stdout / 文件 / 日志**，屏幕只显示掩码；
* 不修改任何登录态文件，只读。

依赖
----
* 本机已登录 WorkBuddy 客户端（登录态为 AES-256-GCM 信封，需借助积分助手技能的
  解密能力；本脚本会自动定位已安装的技能目录）。
* 已认证的 `gh` CLI（`gh auth status` 通过）。
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time

VERSION = "1.0.0"
DEFAULT_REPO = "eastseao/workbuddy-auto-signin"


def _skill_script_dirs():
    """候选技能脚本目录（含 SkillHub 分发后缀），按优先级排列。"""
    home = os.path.expanduser("~")
    base = os.path.join(home, ".workbuddy", "skills")
    cands = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if name.startswith("totorosir-workbuddy-score"):
                cands.append(os.path.join(base, name, "scripts"))
    cands.append(os.path.join(base, "totorosir-workbuddy-score", "scripts"))
    return [p for p in cands if os.path.isfile(os.path.join(p, "rt_auth.py"))]


def _load_rt(skill_dir):
    sys.path.insert(0, skill_dir)
    import rt_auth  # noqa: E402
    tok, path = rt_auth._load_local_rt()
    return tok, path


def mask(s):
    if not s:
        return "<empty>"
    s = str(s)
    return s[:6] + "..." + s[-4:] if len(s) > 14 else "*" * len(s)


def jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p).decode("utf-8", "replace")).get("exp")
    except Exception:
        return None


def gh_path():
    p = shutil.which("gh")
    if p:
        return p
    for c in (os.path.join(os.path.expanduser("~"), ".local", "bin", "gh.exe"),
              r"C:\Program Files\GitHub CLI\gh.exe"):
        if os.path.isfile(c):
            return c
    return None


def gh(args, stdin_text=None):
    r = subprocess.run(args, input=stdin_text, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def set_secret(gh_bin, repo, name, value):
    """令牌只走 stdin，不进 argv。"""
    return gh([gh_bin, "secret", "set", name, "--repo", repo], stdin_text=value + "\n")


def main():
    ap = argparse.ArgumentParser(description="导出本机长效令牌并注入 GitHub Secret")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="目标仓库 owner/name")
    ap.add_argument("--skill-dir", default=None, help="积分助手技能目录（缺省自动定位）")
    ap.add_argument("--webhook", default=None, help="可选：同时注入飞书 webhook 到 FEISHU_WEBHOOK")
    ap.add_argument("--secret", default=None, help="可选：飞书加签密钥，注入 FEISHU_SECRET")
    ap.add_argument("--dry-run", action="store_true", help="只显示将要执行的动作，不写入")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()

    if args.version:
        print("export_token %s" % VERSION)
        return 0

    # ---- 1) 定位技能并读取长效令牌 ----
    dirs = [args.skill_dir] if args.skill_dir else _skill_script_dirs()
    if not dirs:
        print("[FAIL] 未找到积分助手技能目录（totorosir-workbuddy-score）。\n"
              "       请先安装该技能，或用 --skill-dir 指定其 scripts 目录。")
        return 1
    try:
        rt, path = _load_rt(dirs[0])
    except Exception as e:
        print("[FAIL] 读取本机登录态失败：%s" % str(e)[:400])
        print("       请确认 WorkBuddy 客户端已登录（登录态文件存在且未过期）。")
        return 1

    exp = jwt_exp(rt)
    exp_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "未知"
    days = int((exp - time.time()) // 86400) if exp else None
    print("[ok] 登录态文件：%s" % path)
    print("[ok] 长效令牌：%s（长度 %d）" % (mask(rt), len(rt)))
    print("[ok] 令牌到期：%s%s" % (exp_txt, "（还剩 %d 天）" % days if days is not None else ""))
    if days is not None and days < 7:
        print("[warn] 令牌即将过期，建议尽快在客户端重新登录后再次运行本脚本。")

    # ---- 2) 定位 gh ----
    gh_bin = gh_path()
    if not gh_bin:
        print("[FAIL] 未找到 gh CLI，请先安装并 `gh auth login`。")
        return 1
    rc, out, err = gh([gh_bin, "auth", "status"])
    if rc != 0:
        print("[FAIL] gh 未认证：%s" % (err or out))
        return 1
    print("[ok] gh：%s" % out.splitlines()[0] if out else "[ok] gh")

    # ---- 3) 写入 secret ----
    plan = [(args.repo, "WB_REFRESH_TOKEN")]
    if args.webhook:
        plan.append((args.repo, "FEISHU_WEBHOOK"))
    if args.secret:
        plan.append((args.repo, "FEISHU_SECRET"))

    if args.dry_run:
        print("\n[dry-run] 将写入以下 Secret（未执行）：")
        for repo, name in plan:
            print("  - %s :: %s" % (repo, name))
        return 0

    rc, out, err = set_secret(gh_bin, args.repo, "WB_REFRESH_TOKEN", rt)
    if rc != 0:
        print("[FAIL] 写入 WB_REFRESH_TOKEN 失败：%s" % (err or out))
        return 1
    print("[ok] 已注入 %s :: WB_REFRESH_TOKEN（明文未进仓库、未进日志）" % args.repo)

    if args.webhook:
        rc, out, err = set_secret(gh_bin, args.repo, "FEISHU_WEBHOOK", args.webhook)
        print("[%s] FEISHU_WEBHOOK" % ("ok" if rc == 0 else "FAIL"),
              "" if rc == 0 else (err or out))
    if args.secret:
        rc, out, err = set_secret(gh_bin, args.repo, "FEISHU_SECRET", args.secret)
        print("[%s] FEISHU_SECRET" % ("ok" if rc == 0 else "FAIL"),
              "" if rc == 0 else (err or out))

    print("\n完成。可在 Actions 页手动触发一次验证："
          "`gh workflow run daily-signin.yml -R %s`" % args.repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
