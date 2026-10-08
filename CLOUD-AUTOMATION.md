# workbuddy-auto-signin

WorkBuddy「Buddy 加油站」**每日签到 + 派猫猫旅行** 云端自动化，结果推送到**飞书**。

- 运行环境：GitHub Actions 免费定时任务（`ubuntu-latest`），**无需常开设备**
- 一次跑完：签到 →（先领已到家奖励 → 再判断能否派新一趟）→ 汇总推送飞书
- 凭证：本机导出的长效令牌存进仓库 Secret，**明文不进仓库、不进日志**

---

## 1. 目录结构

```
.github/workflows/daily-signin.yml   # 定时工作流（每天 UTC 01:00 == 北京 09:00）
runner/signin.py                     # 云端执行体：签到 + 旅行闭环 + 飞书推送（纯标准库）
tools/export_token.py                # 本机：读登录态导出令牌 → 注入仓库 Secret
tools/export-token.sh / .cmd         # 上面脚本的启动器（command -v 动态取解释器）
```

## 2. 部署（三步）

**① 本机导出令牌并注入 Secret**（在有 WorkBuddy 登录态的电脑上跑；本机需已 `gh auth login`）

```bash
# Git Bash
tools/export-token.sh --repo eastseao/workbuddy-auto-signin
```

```bat
REM Windows CMD
tools\export-token.cmd --repo eastseao/workbuddy-auto-signin
```

带上飞书地址可一并注入：

```bash
tools/export-token.sh --webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxx' [--secret 加签密钥]
```

也可手动设置（值不落盘、不进 shell 历史的话优先用 `gh secret set` 的交互式 stdin）：

```bash
gh secret set WB_REFRESH_TOKEN -R eastseao/workbuddy-auto-signin   # 回车后粘贴令牌，Ctrl+D 结束
gh secret set FEISHU_WEBHOOK  -R eastseao/workbuddy-auto-signin
# 若机器人开了「签名校验」，再加：
gh secret set FEISHU_SECRET   -R eastseao/workbuddy-auto-signin
```

**② 手动触发一次验证**

```bash
gh workflow run daily-signin.yml -R eastseao/workbuddy-auto-signin
gh run watch -R eastseao/workbuddy-auto-signin
```

**③ 之后每天 09:00（北京时间）自动跑**，结果推送到飞书。

## 3. Secret 一览

| Secret | 必填 | 说明 |
|---|---|---|
| `WB_REFRESH_TOKEN` | ✅ | 本机导出的长效令牌（refresh token），有效期实测 30 天 |
| `FEISHU_WEBHOOK` | ✅（要推送时） | 飞书群自定义机器人 Webhook 地址 |
| `FEISHU_SECRET` | ❌ | 飞书机器人开启「签名校验」时才需要 |

> 仓库为 PUBLIC，**任何凭证都只放 Secret**；runner 输出经统一脱敏，令牌只显示 `前6...后4`。

## 4. 接口（2026-10-08 实测，非网络流传的旧名）

| 用途 | 方法 | 地址 |
|---|---|---|
| 换取接口令牌 | POST | `https://copilot.tencent.com/v2/plugin/auth/token/refresh`（头 `X-Refresh-Token` / `X-Auth-Refresh-Source: plugin` / `X-Domain`） |
| 状态查询（只读） | POST | `{domain}/v2/billing/meter/checkin-activity-status` |
| 自动签到（写） | POST | `{domain}/v2/billing/meter/daily-checkin` |
| 旅行状态（只读） | GET | `https://www.workbuddy.cn/activity/growth/buddy/travel/status` |
| 旅行地点配置（只读） | GET | `https://www.workbuddy.cn/activity/growth/buddy/travel/config` |
| 领取旅行奖励（写） | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/claim` |
| 派遣旅行（写） | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/depart` |

要点：
- `{domain}` **取刷新应答里的 `data.domain`**（实测 `copilot.tencent.com`），**不要硬编码**。
- 旅行接口族路径**不带 `/v2` 前缀**，域名固定 `www.workbuddy.cn`。
- 写操作只有 3 个：签到、领取、派遣。**不碰**兑换/抽奖等其它写接口。

## 5. 行为约定

- **幂等**：签到先查 `today_checked_in` 再决定是否发写请求；接口 `code=10001`（今天已签到）视为成功。
- **不丢积分**：到达状态会一直保持 `arrived`，下次运行自动补领。
- **不超发**：派遣前必查 `daily_limit_reached`，达上限**不发写请求**。
- **失败隔离**：猫猫整段独立 `try/except`，**猫猫失败不影响签到结论**；签到成功 → 退出码 0。
- **退出码**：`0` 签到成功（含「今日已签」）；`1` 签到失败 / 令牌无效 / 缺凭证。

## 6. 维护

- 长效令牌**有效期实测 30 天**（`refreshExpiresIn=2592000`）。到期后工作流会报
  `长效令牌刷新失败`，届时重跑一次 `tools/export-token.sh` 即可续期。
- 到期前 7 天，`export_token.py` 会主动提示；飞书卡片也会带上签到失败原因。
- GitHub 定时任务在仓库连续 60 天无活动后会被自动暂停，届时到 Actions 页面点一次
  `Enable` 即可。

## 7. 本地演练

```bash
# 只读演练（不发任何写请求），把报告写到当前目录
WB_REFRESH_TOKEN=<令牌> python3 runner/signin.py --dry-run --json-out report.json
```
