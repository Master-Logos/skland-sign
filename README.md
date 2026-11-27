# Skland-Sign-In

森空岛自动签到脚本，用于《明日方舟》和《终末地》的每日签到。

本项目已改造为 **GitHub Actions 定时运行**，无需 Docker、无需服务器常驻。

fork from https://github.com/kafuneri/Skland-Sign-In

## 功能特性

- 支持单账号和多账号签到
- 支持 GitHub Secrets 管理 Token
- 支持手动触发和定时触发
- 支持多种通知渠道（Qmsg / OneBot / 邮件 / 企业微信 / 微信服务号 / Server酱）

## 1. 如何获取 Token

1. 登录森空岛官网：https://www.skland.com/
2. 打开：https://web-api.skland.com/account/info/hg
3. 复制返回 JSON 中 `content` 字段的完整字符串，作为签到 Token

示例：

```json
{"code":0,"data":{"content":"这里是一长串Token"}}
```

## 2. GitHub Actions 使用

工作流文件：`.github/workflows/sign-in.yml`

### 一键创建自己的仓库
1. 使用模板创建仓库
[![Use this template](https://img.shields.io/badge/GitHub-Use%20this%20template-success?logo=github)](https://github.com/new?template_name=skland_auto_login&template_owner=NatsumiXD)

2. 创建“Repository secrets”
[![Create the secret](https://img.shields.io/badge/GitHub-Create%20the%20secret-success?logo=github)](./../../settings/secrets/actions)

## 3. 配置仓库 Secrets

路径：`Settings -> Secrets and variables -> Actions -> New repository secret`

支持以下三种方式，脚本按优先级读取：

1. `SKLAND_USERS_JSON`（优先级最高）
2. `SKLAND_TOKENS`
3. `SKLAND_TOKEN`

### 方式 A：单账号

Secret 名称：`SKLAND_TOKEN`

值：

```text
你的Token
```

### 方式 B：多账号（简单）

Secret 名称：`SKLAND_TOKENS`

值可用换行或逗号分隔：

```text
token_1
token_2
```

或

```text
token_1,token_2
```

### 方式 C：多账号 + 自定义昵称（推荐）

Secret 名称：`SKLAND_USERS_JSON`

值：

```json
[
  {"nickname": "大号", "token": "token_1"},
  {"nickname": "小号", "token": "token_2"}
]
```

## 4. 运行方式

### 手动运行

1. 打开仓库 `Actions`
2. 选择 `Skland Sign In`
3. 点击 `Run workflow`

### 定时运行

默认每天自动执行一次，无需额外操作。

如果要修改时间，编辑 `.github/workflows/sign-in.yml` 里的 `cron`：

```yaml
schedule:
  - cron: "0 17 * * *"
```

说明：GitHub Actions 使用 UTC 时区。

## 5. 本地运行（可选）

如果你希望本地测试：

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 配置账号（二选一）

- 使用环境变量：`SKLAND_TOKEN` / `SKLAND_TOKENS` / `SKLAND_USERS_JSON`
- 或复制 `config.example.yaml` 为 `config.yaml`，填写 `users`

3. 执行

```bash
python main.py
```

## 6. 通知配置（可选）

通知配置在 `config.yaml` 的 `notify` 节点中，按需填写即可启用。

支持渠道：

- Qmsg
- OneBot V11
- 邮件 SMTP
- 企业微信机器人
- 微信服务号模板消息
- Server酱

## 7. 常见问题

### 1) 工作流运行了但没有签到

请先检查：

- Secret 是否正确配置
- Token 是否有效（是否过期）
- Actions 日志中是否有账号解析失败信息

### 2) 多账号没有按预期显示昵称

请使用 `SKLAND_USERS_JSON`，并确保 JSON 格式合法。

## 致谢

本项目核心 API 交互逻辑（`skland_api.py`）提取自 AstrBot 开源插件：
https://github.com/Azincc/astrbot_plugin_skland

## 8. WorkBuddy 每日签到与积分日报

`daily-sign.yml` 现在一次跑**三个站**：森空岛、镜客居、WorkBuddy，汇总成一封邮件。
WorkBuddy 的部分由 `wb_sign.py` 负责，除了签到，还会报出剩余积分和到期时间：

```
📅 WorkBuddy 签到
✅ 签到成功，+50 分
💎 剩余 1,130.17 分（套餐 230.17 · 奖励 900）
🔄 套餐 11/30 23:59 刷新，届时 230.17 分清零（3 天后）
⏳ 最近到期 12/23 16:44，100 分（25 天后）
```

### 需要新增的两个 Secrets

| 名称 | 值 |
|---|---|
| `WB_SESSION` | WorkBuddy 登录凭据 JSON，即本机看板 `data/session.json` 的完整内容 |
| `WB_STATE_KEY` | 任意一长串随机字符（当作加密口令），例如 32 位以上随机串 |

### 为什么需要 `WB_STATE_KEY`

WorkBuddy 的 refresh token 有效期 30 天，且**每次续期都会轮换**。所以每天运行结束后，
脚本必须把新凭据写回去，否则一个月后就会失效。

写回的是 `state/wb_session.enc` —— 用 `WB_STATE_KEY` 做 AES-256-GCM 加密后的密文。
因此即使这个仓库是公开的，也不会泄露明文凭据（工作流每次运行后会自动提交这个文件，
提交信息带 `[skip ci]`）。

`WB_SESSION` 只在**第一次**运行（或 `state/wb_session.enc` 被删掉）时使用，
之后就靠加密状态文件滚动续期。

### 登录失效了怎么办

邮件里会出现 `❌ 登录凭据续期失败`，并在「需要你处理」中提示。此时：

1. 在笔记本上运行 `buddy-credits` 看板，重新登录一次；
2. 把新的 `data/session.json` 内容覆盖到 `WB_SESSION` Secret；
3. 删除仓库里的 `state/wb_session.enc`，手动 Run workflow 一次即可恢复。

### 邮箱去重

同一天里多次触发（例如 cron-job.org 配了多个错峰点）时，只有第一次会发邮件：
`wb_sign.py` 会把「今天已经汇报过」记进加密状态，后续运行视为重复。
手动 Run workflow 并勾选 `force_email` 则不受此限制。
