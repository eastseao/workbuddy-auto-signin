#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy「Buddy 加油站」每日自动化 · 云端 runner
=================================================

在 GitHub Actions runner（或任意能联网的机器）上执行：
  1) 用长效令牌(refresh token)换取接口令牌(accessToken)，令牌只从环境变量读；
  2) 自动签到（幂等，先查后签）；
  3) 派猫猫旅行闭环：**先领已到家奖励 → 再判断能否派新一趟**（已达每日上限则不派）；
  4) 把「签到」与「猫猫旅行」两块结果分开写清楚，推送到飞书自定义机器人。

设计约定
--------
* 仅标准库，无第三方依赖；不用 `datetime.now()` 之外的外部时钟。
* **不依赖任何本机登录态**：只认环境变量 `WB_REFRESH_TOKEN`。
* 任何输出（stdout / 日志 / 推送内容）都**不含令牌明文**：统一经 `_scrub()` 脱敏。
* 「猫猫旅行」整段包在独立 try/except 内：**猫猫失败不影响签到结论**，
  签到成功即整体成功（退出码 0）。

接口名（2026-10-08 本机实测，非网络流传的旧名）
----------------------------------------------
凭证刷新  POST https://copilot.tencent.com/v2/plugin/auth/token/refresh
          头 X-Refresh-Token / X-Auth-Refresh-Source: plugin / X-Domain
状态查询  POST {domain}/v2/billing/meter/checkin-activity-status   body {}
自动签到  POST {domain}/v2/billing/meter/daily-checkin             body {}
          （{domain} 取刷新应答里的 data.domain，实测为 copilot.tencent.com）
旅行只读  GET  https://www.workbuddy.cn/activity/growth/buddy/travel/status
          GET  https://www.workbuddy.cn/activity/growth/buddy/travel/config
旅行写    POST https://www.workbuddy.cn/activity/growth/buddy/travel/claim   body {}
          POST https://www.workbuddy.cn/activity/growth/buddy/travel/depart  body {"location_id": N}
          ★ 旅行接口族路径**不带 /v2 前缀**，域名固定 www.workbuddy.cn

退出码
------
  0  签到成功（含「今日已签」幂等情形；猫猫失败不改变结论）
  1  签到失败 / 令牌无效 / 缺少凭证
  2  参数错误
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"

# ---------------------------------------------------------------- 常量（实测）

REFRESH_URL = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
REFRESH_DOMAIN = "copilot.tencent.com"
FALLBACK_DOMAIN = "copilot.tencent.com"

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"

TRAVEL_BASE = "https://www.workbuddy.cn"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"

TRAVEL_STATE_TEXT = {"idle": "空闲", "traveling": "旅行中", "arrived": "已到家待领取"}

HTTP_TIMEOUT = 25
UA = "workbuddy-auto-signin/1.0 (+github-actions)"

_SECRET_KEYS = ("accesstoken", "refreshtoken", "sessionstate", "id_token",
                "access_token", "refresh_token", "token", "secret", "password")
_LONG_JWT_RE = None


# ---------------------------------------------------------------- 脱敏工具

def mask(s):
    """把一串敏感字符串变成 `前6...后4` 形式；不泄露长度以外的信息。"""
    if not s:
        return "<empty>"
    s = str(s)
    if len(s) <= 14:
        return "*" * len(s)
    return "%s...%s" % (s[:6], s[-4:])


def _looks_like_jwt(v):
    return (isinstance(v, str) and len(v) >= 120 and v.count(".") == 2
            and v.replace(".", "").replace("-", "").replace("_", "").isalnum())


def _scrub(obj):
    """递归脱敏：命中敏感键或形似 JWT 的字符串一律替换为掩码。可幂等重复调用。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out[k] = "<masked>" if not isinstance(v, str) else "<masked:%s>" % mask(v)
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, str) and obj.startswith("<masked"):
        return obj
    if _looks_like_jwt(obj):
        return "<masked:%s>" % mask(obj)
    return obj


def _log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- HTTP

def http_json(method, url, token=None, body=None, extra_headers=None, timeout=HTTP_TIMEOUT):
    """发一次请求，返回 (http_status, parsed_or_text, elapsed_ms)。异常也返回，不抛。"""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            raw, status = e.read().decode("utf-8", "replace"), e.code
        except Exception:
            raw, status = "", e.code
    except Exception as e:                                     # 网络层
        raw, status = json.dumps({"__transport_error__": str(e)[:200]}), -1
    ms = int((time.time() - t0) * 1000)

    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {"__non_json__": raw[:800]}
    return status, parsed, ms


# ---------------------------------------------------------------- 凭证

def refresh_access_token(rt):
    """长效令牌 -> (access_token, domain, refresh_token, expiresIn, refreshExpiresIn, raw)。"""
    status, body, ms = http_json(
        "POST", REFRESH_URL, body={},
        extra_headers={
            "X-Refresh-Token": rt,
            "X-Auth-Refresh-Source": "plugin",
            "X-Domain": REFRESH_DOMAIN,
        })
    data = body.get("data") if isinstance(body, dict) else None
    info = {
        "step": "refresh", "url": REFRESH_URL, "http": status, "ms": ms,
        "code": body.get("code") if isinstance(body, dict) else None,
        "msg": body.get("msg") if isinstance(body, dict) else None,
        "response_fields": sorted(data.keys()) if isinstance(data, dict) else None,
        "domain": (data or {}).get("domain"),
        "expiresIn": (data or {}).get("expiresIn"),
        "refreshExpiresIn": (data or {}).get("refreshExpiresIn"),
    }
    if status != 200 or not isinstance(data, dict) or body.get("code") != 0:
        return None, info, _scrub(body)
    at = data.get("accessToken") or ""
    if not at:
        return None, info, _scrub(body)
    return {
        "access_token": at,
        "domain": data.get("domain") or FALLBACK_DOMAIN,
        "refresh_token": data.get("refreshToken") or "",
        "expires_in": data.get("expiresIn"),
        "refresh_expires_in": data.get("refreshExpiresIn"),
    }, info, _scrub(body)


# ---------------------------------------------------------------- 签到

def parse_status(body):
    d = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
    return {
        "today_checked_in": bool(d.get("today_checked_in")),
        "streak_days": d.get("streak_days"),
        "total_credits": d.get("total_credits"),
        "daily_credit": d.get("daily_credit"),
        "activity_name": d.get("activity_name"),
        "week_checkin_days": d.get("week_checkin_days"),
        "active": d.get("active"),
    }


def do_checkin(domain, at, dry_run=False):
    """先查后签（幂等）。返回 (success, result_dict)。"""
    base = "https://" + domain
    res = {"success": False, "result": "failed", "summary": "",
           "raw": {}, "error": None,
           "before": None, "streak_days": None, "total_credits": None}

    st, st_body, st_ms = http_json("POST", base + STATUS_PATH, token=at, body={})
    res["raw"]["status_request"] = {
        "method": "POST", "url": base + STATUS_PATH, "http": st, "ms": st_ms}
    res["raw"]["status_response"] = _scrub(st_body)

    if st == 200 and isinstance(st_body, dict) and st_body.get("code") == 0:
        info = parse_status(st_body)
        res["before"] = info
        res["streak_days"] = info["streak_days"]
        res["total_credits"] = info["total_credits"]
    elif st == -1:
        res["error"] = "状态查询网络异常"
    else:
        res["error"] = "状态查询异常 HTTP %s" % st

    if res["before"] and res["before"]["today_checked_in"]:
        res.update(success=True, result="already",
                   summary="今日已签到（幂等跳过，未重复发奖）")
        return True, res

    if dry_run:
        res.update(success=True, result="dry_run",
                   summary="[dry-run] 未登录态为未签到，跳过实际签到请求")
        return True, res

    ck_status, ck_body, ck_ms = http_json("POST", base + CHECKIN_PATH, token=at, body={})
    res["raw"]["checkin_request"] = {
        "method": "POST", "url": base + CHECKIN_PATH, "http": ck_status, "ms": ck_ms}
    res["raw"]["checkin_response"] = _scrub(ck_body)

    code = str(ck_body.get("code", "")) if isinstance(ck_body, dict) else ""
    msg = (ck_body.get("msg") or "") if isinstance(ck_body, dict) else ""
    d = ck_body.get("data") if isinstance(ck_body, dict) and isinstance(ck_body.get("data"), dict) else {}

    if code == "10001" or "已签到" in msg:
        res.update(success=True, result="already",
                   summary="今日已签到（接口 code=10001，幂等）")
        return True, res

    if 200 <= ck_status < 300 and code in ("", "0", "200"):
        credit = d.get("credit") or d.get("daily_credit") or d.get("today_credit")
        if credit is None and res["before"]:
            credit = res["before"].get("daily_credit")
        res["streak_days"] = d.get("streak_days") or res["streak_days"]
        for key in ("total_credits", "balance", "credits"):
            if isinstance(d.get(key), (int, float)):
                res["total_credits"] = d[key]
                break
        res.update(success=True, result="checked_in",
                   summary="签到成功" + ("，+%s 积分" % credit if credit else ""))
        res["credit"] = credit
        return True, res

    res["error"] = msg or ("HTTP %s (code=%s)" % (ck_status, code))
    res["summary"] = "签到失败：" + res["error"]
    return False, res


# ---------------------------------------------------------------- 猫猫旅行

def travel_get(at, path):
    st, body, ms = http_json("GET", TRAVEL_BASE + path, token=at)
    ok = (st == 200 and isinstance(body, dict) and body.get("code") == 0)
    return {
        "http": st, "ms": ms, "ok": ok,
        "data": body.get("data") if ok else None,
        "response": _scrub(body),
    }


def travel_post(at, path, payload):
    st, body, ms = http_json("POST", TRAVEL_BASE + path, token=at, body=payload)
    ok = (st == 200 and isinstance(body, dict) and body.get("code") == 0)
    return {
        "http": st, "ms": ms, "ok": ok,
        "data": body.get("data") if ok else None,
        "response": _scrub(body),
    }


def _fmt_remain(sec):
    if sec is None:
        return None
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h:
        return "%d 小时 %d 分" % (h, m)
    if m:
        return "%d 分钟" % m
    return "%d 秒" % sec


def do_travel(at, dry_run=False, location_id=None):
    """先领后派闭环。返回 dict；任何内部异常都收敛进 error，不向上抛。"""
    res = {"ok": False, "actions": [], "state_before": None, "state_after": None,
           "state_text": None, "location_name": None, "remaining_text": None,
           "reward_credit": None, "daily_limit_reached": None,
           "summary": "", "raw": {}, "error": None}
    try:
        cfg = travel_get(at, TRAVEL_CONFIG_PATH)
        locations = ((cfg.get("data") or {}).get("locations") or []) if cfg["ok"] else []
        res["locations"] = [{"id": l.get("id"), "name": l.get("name")} for l in locations]
        # 只留「可选地点」的 id/name，避免把整包配图塞进报告
        res["raw"]["config"] = {"http": cfg["http"], "ok": cfg["ok"],
                                "locations": res["locations"]}

        st0 = travel_get(at, TRAVEL_STATUS_PATH)
        res["raw"]["status_before"] = st0["response"]
        if not st0["ok"]:
            res["error"] = "旅行状态查询失败（HTTP %s）" % st0["http"]
            res["summary"] = "猫猫：状态查询失败"
            return res

        sd = st0["data"] or {}
        res["state_before"] = sd.get("state")
        res["daily_limit_reached"] = sd.get("daily_limit_reached")
        loc = sd.get("location") if isinstance(sd.get("location"), dict) else {}
        res["location_name"] = loc.get("name")

        state = sd.get("state")

        # ① 已到家 → 先领
        if state == "arrived":
            if dry_run:
                res["actions"].append({"action": "claim", "ok": True, "dry_run": True,
                                       "detail": "[dry-run] 跳过领取"})
            else:
                cl = travel_post(at, TRAVEL_CLAIM_PATH, {})
                res["raw"]["claim"] = cl["response"]
                credit = (cl.get("data") or {}).get("reward_credit")
                res["actions"].append({
                    "action": "claim", "ok": cl["ok"],
                    "reward_credit": credit,
                    "detail": ("已领取旅行奖励 +%s 积分" % credit) if cl["ok"]
                              else "领取失败 HTTP %s" % cl["http"]})
                if cl["ok"]:
                    res["reward_credit"] = credit
                st1 = travel_get(at, TRAVEL_STATUS_PATH)
                res["raw"]["status_after_claim"] = st1["response"]
                if st1["ok"]:
                    sd = st1["data"] or {}
                    res["state_after"] = sd.get("state")
                    res["daily_limit_reached"] = sd.get("daily_limit_reached")
                    state = sd.get("state")
                else:
                    state = None

        # ② 空闲且未达上限 → 派新的一趟
        if state == "idle":
            if res["daily_limit_reached"]:
                res["actions"].append({"action": "depart", "ok": True, "skipped": True,
                                       "detail": "今日派遣已达上限，跳过（不发送写请求）"})
            elif dry_run:
                res["actions"].append({"action": "depart", "ok": True, "dry_run": True,
                                       "detail": "[dry-run] 跳过派遣"})
            else:
                chosen = location_id
                if chosen is None and locations:
                    chosen = random.choice([l.get("id") for l in locations
                                            if l.get("id") is not None])
                dp = travel_post(at, TRAVEL_DEPART_PATH, {"location_id": chosen})
                res["raw"]["depart"] = dp["response"]
                nloc = (dp.get("data") or {}).get("location") or {}
                res["actions"].append({
                    "action": "depart", "ok": dp["ok"], "location_id": chosen,
                    "detail": ("已派出新的一趟 → %s" % (nloc.get("name") or chosen)) if dp["ok"]
                              else "派遣失败 HTTP %s" % dp["http"]})
                if dp["ok"]:
                    st2 = travel_get(at, TRAVEL_STATUS_PATH)
                    res["raw"]["status_after_depart"] = st2["response"]
                    if st2["ok"]:
                        sd = st2["data"] or {}
                        res["state_after"] = sd.get("state")
                        res["location_name"] = (sd.get("location") or {}).get("name") or res["location_name"]
                        state = sd.get("state")

        # ③ 旅行中 → 只报倒计时
        if state == "traveling":
            arrive, now = sd.get("arrive_at"), sd.get("server_now")
            if res.get("state_after") is None:
                res["state_after"] = "traveling"
            if isinstance(arrive, int) and isinstance(now, int):
                rem = max(0, arrive - now)
                res["remaining_text"] = _fmt_remain(rem)
                res["remaining_seconds"] = rem

        res["state_text"] = TRAVEL_STATE_TEXT.get(res["state_after"] or res["state_before"],
                                                  res["state_after"] or res["state_before"])
        acts = res["actions"]
        failed = [a for a in acts if not a.get("ok") and not a.get("skipped")]
        if failed:
            res["ok"] = False
            res["error"] = failed[0].get("detail")
            res["summary"] = "猫猫：执行失败 —— %s" % res["error"]
            return res

        res["ok"] = True
        parts = [a["detail"] for a in acts]
        after = res["state_after"] or res["state_before"]
        if after == "traveling":
            tail = "旅行中（%s），预计还有 %s 到家" % (
                res.get("location_name") or "?", res.get("remaining_text") or "?")
            if res["daily_limit_reached"]:
                tail += "；今日已派过，不重复派遣"
            parts.append(tail)
        elif after == "arrived" and not parts:
            parts.append("已到家但本次未领取成功，下次运行会自动补领")
        elif after == "idle" and not parts:
            parts.append("空闲且无待领取（本轮未派遣）")
        res["summary"] = "猫猫：" + "；".join(parts or ["无需操作"])
        return res
    except Exception as e:                                       # 兜底：绝不影响签到
        res["ok"] = False
        res["error"] = "猫猫模块异常：%s" % str(e)[:200]
        res["summary"] = "猫猫：模块异常，已隔离（不影响签到结论）"
        return res


# ---------------------------------------------------------------- 飞书推送

def feishu_sign(secret, timestamp):
    """飞书加签：base64(hmac_sha256(secret, f"{ts}\\n{secret}"))，ts 为秒。"""
    key = ("%s\n%s" % (timestamp, secret)).encode("utf-8")
    return base64.b64encode(hmac.new(secret.encode("utf-8"), key, hashlib.sha256).digest()).decode()


def build_feishu_card(report):
    ck, tv = report["checkin"], report["travel"]

    ck_icon = {"checked_in": "✅", "already": "ℹ️", "dry_run": "🧪"}.get(ck["result"], "❌")
    ck_lines = ["%s **签到：%s**" % (ck_icon, ck["summary"] or ck["result"])]
    if ck.get("streak_days") is not None:
        ck_lines.append("连续登录：**%s 天**" % ck["streak_days"])
    if ck.get("total_credits") is not None:
        ck_lines.append("本活动期累计积分：**%s**" % ck["total_credits"])
    if not ck["success"] and ck.get("error"):
        ck_lines.append("失败原因：`%s`" % ck["error"])

    if tv.get("error") and not tv.get("actions"):
        tv_lines = ["⚠️ **猫猫旅行：未取到数据**", "原因：`%s`" % tv["error"],
                    "_猫猫失败已隔离，不影响上面的签到结论_"]
    else:
        head = {"traveling": "🚶", "arrived": "🏠", "idle": "😴"}.get(tv.get("state_after"), "❔")
        tv_lines = ["%s **猫猫旅行：%s**" % (head, tv.get("state_text") or "未知")]
        if tv.get("location_name"):
            tv_lines.append("当前地点：%s" % tv["location_name"])
        if tv.get("remaining_text"):
            tv_lines.append("预计到达：还有 %s" % tv["remaining_text"])
        for a in tv.get("actions", []):
            mark = "✅" if a.get("ok") else "❌"
            if a.get("skipped"):
                mark = "⏭️"
            tv_lines.append("%s %s" % (mark, a.get("detail")))
        if tv.get("state_after") == "arrived":
            tv_lines.append("_已到家但未领取成功，下次运行会自动补领_")
        if tv.get("error"):
            tv_lines.append("⚠️ %s" % tv["error"])
            tv_lines.append("_猫猫失败已隔离，不影响签到结论_")

    template = "green" if ck["success"] else "red"
    if ck["success"] and not tv.get("ok"):
        template = "orange"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text",
                          "content": "WorkBuddy 日报 · %s" % report["run_date"]},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(ck_lines)}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(tv_lines)}},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": "runner v%s · %s · GitHub Actions" % (VERSION, report["run_at"])}]},
            ],
        },
    }


def build_feishu_text(report):
    ck, tv = report["checkin"], report["travel"]
    lines = ["WorkBuddy 日报 · %s" % report["run_date"], "",
             "【签到】%s" % (ck["summary"] or ck["result"])]
    if ck.get("streak_days") is not None:
        lines.append("连续登录 %s 天；本活动期累计积分 %s" % (ck["streak_days"], ck.get("total_credits")))
    if not ck["success"] and ck.get("error"):
        lines.append("失败原因：%s" % ck["error"])
    lines.append("")
    if tv.get("error") and not tv.get("actions"):
        lines.append("【猫猫旅行】未取到数据：%s" % tv["error"])
    else:
        lines.append("【猫猫旅行】%s" % (tv.get("state_text") or "-"))
        for a in tv.get("actions", []):
            lines.append("  - %s" % a.get("detail"))
        if tv.get("error"):
            lines.append("  ! %s" % tv["error"])
    return "\n".join(lines)


def push_feishu(report, webhook, secret=None):
    if not webhook:
        return {"channel": "feishu", "status": "unconfigured",
                "detail": "未配置 FEISHU_WEBHOOK，跳过推送（结果见本页日志/报告文件）"}
    url = webhook
    if secret:
        ts = str(int(time.time()))
        url = "%s%s%s=%s&timestamp=%s" % (
            webhook, "&" if "?" in webhook else "?", "sign",
            urllib.parse.quote(feishu_sign(secret, ts), safe=""), ts)
    payload = build_feishu_card(report)
    status, body, ms = http_json("POST", url, body=payload, timeout=15)
    code = body.get("code") if isinstance(body, dict) else None
    ok = (status == 200 and code == 0)
    return {"channel": "feishu", "status": "ok" if ok else "failed",
            "http_code": status, "ms": ms,
            "detail": "已推送到飞书" if ok
                      else ("飞书返回 %s" % json.dumps(_scrub(body), ensure_ascii=False)[:300])}


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="WorkBuddy 每日签到 + 派猫猫（云端 runner）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只读演练：不发送任何写请求（签到/领取/派遣）")
    ap.add_argument("--no-push", action="store_true", help="不推送飞书")
    ap.add_argument("--location", type=int, default=None, help="指定派遣地点 id（1-4），缺省随机")
    ap.add_argument("--json-out", default=None, help="把完整脱敏报告写到该路径")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)

    if args.version:
        _log("workbuddy-auto-signin runner %s" % VERSION)
        return 0

    rt = (os.environ.get("WB_REFRESH_TOKEN") or "").strip()
    webhook = (os.environ.get("FEISHU_WEBHOOK") or "").strip()
    fsecret = (os.environ.get("FEISHU_SECRET") or "").strip() or None

    now = time.localtime()
    report = {
        "runner_version": VERSION,
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S", now),
        "run_date": time.strftime("%Y-%m-%d", now),
        "dry_run": bool(args.dry_run),
        "ok": False,
        "checkin": None,
        "travel": None,
        "push": None,
    }

    if not rt:
        report["checkin"] = {"success": False, "result": "failed",
                             "summary": "未读到 WB_REFRESH_TOKEN", "error": "missing_credential",
                             "raw": {}, "streak_days": None, "total_credits": None}
        report["travel"] = {"ok": False, "actions": [], "error": "missing_credential",
                            "summary": "未执行（缺少凭证）"}
        _emit(report, args)
        return 1

    # ---- 1) 换接口令牌 ----
    cred, info, raw_refresh = refresh_access_token(rt)
    report["auth"] = {"masked_refresh_token": mask(rt), "refresh_request": info,
                      "refresh_response": raw_refresh}
    _log("[auth] refresh -> HTTP %s code=%s domain=%s expiresIn=%s refreshExpiresIn=%s"
         % (info["http"], info["code"], info["domain"], info["expiresIn"], info["refreshExpiresIn"]))
    _log("[auth] access token: %s" % mask(cred["access_token"] if cred else ""))

    if not cred:
        report["checkin"] = {"success": False, "result": "failed",
                             "summary": "长效令牌刷新失败（可能已过期或无效）",
                             "error": "refresh_failed", "raw": {}, "streak_days": None,
                             "total_credits": None}
        report["travel"] = {"ok": False, "actions": [], "error": "refresh_failed",
                            "summary": "未执行（令牌刷新失败）"}
        report["push"] = ({"channel": "feishu", "status": "skipped", "detail": "--no-push"}
                          if args.no_push else push_feishu(report, webhook, fsecret))
        _emit(report, args)
        return 1

    at, domain = cred["access_token"], cred["domain"]

    # ---- 2) 签到（决定整体成败）----
    ck_ok, ck = do_checkin(domain, at, dry_run=args.dry_run)
    report["checkin"] = ck
    report["ok"] = bool(ck_ok)
    _log("[checkin] %s | %s" % (ck["result"], ck["summary"]))

    # ---- 3) 猫猫旅行（失败隔离）----
    report["travel"] = do_travel(at, dry_run=args.dry_run, location_id=args.location)
    _log("[travel] %s" % report["travel"].get("summary"))

    # ---- 4) 推送 ----
    if args.no_push:
        report["push"] = {"channel": "feishu", "status": "skipped", "detail": "--no-push"}
    else:
        report["push"] = push_feishu(report, webhook, fsecret)
    _log("[push] %s %s" % (report["push"].get("status"), report["push"].get("detail")))

    _emit(report, args)
    return 0 if report["ok"] else 1


def _emit(report, args):
    _log("")
    _log("=========== 脱敏报告 (JSON) ===========")
    out = json.dumps(_scrub(report), ensure_ascii=False, indent=2)
    _log(out)
    if args.json_out:
        try:
            with open(args.json_out, "w", encoding="utf-8") as f:
                f.write(out)
        except Exception as e:
            _log("[warn] 写报告文件失败: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
