# daily_sign.py
"""统一签到编排：依次完成森空岛与镜客居签到，汇总成一封邮件。

复用仓库里现有的 skland_api.py / notifier.py / jkju/main.py，不改动它们的签到逻辑。
每个站点内部最多尝试 2 次（间隔 60 秒），任一成功或"已签到"即视为完成。
邮件标题直接标注各站成败，正文附带需要用户介入时的具体操作指引。
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import yaml

from notifier import EmailNotifier
from skland_api import SklandAPI, ALREADY_SIGNED_KEYWORDS
from jkju.main import AutoSign

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DailySign")

RETRY_DELAY = 60  # 秒


# ---------------------------------------------------------------- 结果结构

@dataclass
class SiteResult:
    """单个站点的签到结果。"""
    name: str            # 站点名
    ok: bool             # 成功或已签到
    status: str          # ok / captcha / login_failed / site_changed / no_account / error
    text: str            # 邮件正文中的详细内容
    action: str = ""     # 需要用户介入时的具体操作指引（空表示无需操作）
    changed: bool = False  # 本次是否真的发生了签到动作（False=今天本来就签过了）


# 状态 -> (标题图标, 标题里的简短原因)
STATUS_META = {
    "ok": ("✅", ""),
    "captcha": ("⚠️", "验证码"),
    "login_failed": ("❌", "登录失败"),
    "site_changed": ("❌", "站点改版"),
    "no_account": ("❌", "未配置"),
    "error": ("❌", "异常"),
}


def build_subject(results: list[SiteResult]) -> str:
    """标题直接写出每个站的成败，例如：✅森空岛 ⚠️镜客居·验证码。"""
    parts = []
    for r in results:
        icon, short = STATUS_META.get(r.status, ("❓", ""))
        label = f"{icon}{r.name}"
        if short:
            label += f"·{short}"
        parts.append(label)
    return " ".join(parts)


def build_message(results: list[SiteResult]) -> str:
    """正文包含详细结果；有需要用户处理的事项时，末尾列出具体操作。"""
    lines = ["📅 每日签到汇总", "════════════════", ""]
    for i, r in enumerate(results):
        if i:
            lines += ["────────────────", ""]
        lines += r.text.split("\n")
        lines.append("")

    pending = [(r.name, r.action) for r in results if r.action]
    lines.append("════════════════")
    if pending:
        lines += ["⚠️ 需要你处理", ""]
        for name, action in pending:
            lines.append(f"• {name}：{action}")
            lines.append("")
    else:
        lines.append("✅ 两个站都正常，无需操作。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 森空岛

def _load_config() -> dict:
    if not os.path.exists("config.yaml"):
        return {}
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.error(f"读取 config.yaml 失败: {exc}")
        return {}


def _parse_skland_users(config: dict) -> list[dict]:
    """与 main.py 一致的账号优先级：USERS_JSON > TOKENS > TOKEN > config.yaml。"""
    raw = os.getenv("SKLAND_USERS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            users = [
                {
                    "nickname": item.get("nickname", f"账号{idx}"),
                    "token": str(item.get("token", "")).strip(),
                }
                for idx, item in enumerate(parsed, 1)
                if isinstance(item, dict) and item.get("token")
            ]
            if users:
                return users
        except Exception as exc:
            logger.error(f"解析 SKLAND_USERS_JSON 失败: {exc}")

    tokens_raw = os.getenv("SKLAND_TOKENS", "").strip()
    if tokens_raw:
        tokens = [t.strip() for t in tokens_raw.replace(",", "\n").splitlines() if t.strip()]
        if tokens:
            return [{"nickname": f"账号{i}", "token": t} for i, t in enumerate(tokens, 1)]

    single = os.getenv("SKLAND_TOKEN", "").strip()
    if single:
        return [{"nickname": "账号1", "token": single}]

    return config.get("users", [])


async def _sign_skland_once(config: dict) -> SiteResult:
    """执行一次森空岛签到。"""
    users = _parse_skland_users(config)
    if not users:
        return SiteResult(
            "森空岛", False, "no_account",
            "📅 森空岛签到姬\n❌ 未配置账号（SKLAND_TOKEN(S) 或 config.yaml）",
            "在仓库 Settings → Secrets and variables → Actions 中添加 SKLAND_TOKEN（或 SKLAND_USERS_JSON）。",
        )

    lines = ["📅 森空岛签到姬", ""]
    all_ok = True
    token_error = False
    did_sign = False  # 本次是否实际完成了新签到（而非本来就已签）
    api = SklandAPI(max_retries=3)
    try:
        for idx, user in enumerate(users, 1):
            nickname = user.get("nickname", "未知用户")
            token = user.get("token")
            lines.append(f"🌈 No.{idx}({nickname}):")
            if not token:
                lines.append("❌ 账号配置错误: 缺少Token")
                lines.append("")
                all_ok, token_error = False, True
                continue
            try:
                results, _official = await api.do_full_sign_in(token)
                if not results:
                    lines.append("❌ 未找到绑定角色")
                    all_ok = False
                for r in results:
                    # 统一使用 skland_api 的关键词集合，避免与 _is_signed_today 口径不一致
                    already = not r.success and any(
                        k in r.error for k in ALREADY_SIGNED_KEYWORDS
                    )
                    if r.success:
                        did_sign = True
                        detail = f" ({', '.join(r.awards)})" if r.awards else ""
                        line = f"✅ {r.game}: 成功{detail}"
                    elif already:
                        line = f"✅ {r.game}: 已签"
                    else:
                        line = f"❌ {r.game}: 失败 ({r.error})"
                        all_ok = False
                        if any(k in r.error for k in ["token", "Token", "鉴权", "登录", "401", "403"]):
                            token_error = True
                    lines.append(line)
                    logger.info(f"  - {line}")
            except Exception as exc:
                lines.append(f"❌ 系统错误: {exc}")
                all_ok = False
            lines.append("")
    finally:
        await api.close()

    while lines and lines[-1] == "":
        lines.pop()
    text = "\n".join(lines)

    if all_ok:
        return SiteResult("森空岛", True, "ok", text, changed=did_sign)
    if token_error:
        return SiteResult(
            "森空岛", False, "login_failed", text,
            "Token 可能已过期或失效，请重新获取后更新 SKLAND_TOKEN / SKLAND_USERS_JSON Secret。",
        )
    return SiteResult(
        "森空岛", False, "error", text,
        "签到接口返回异常（非 Token 问题），请查看 Actions 运行日志，必要时把日志发给维护者。",
    )


async def sign_skland(config: dict) -> SiteResult:
    result = await _sign_skland_once(config)
    if result.ok:
        return result
    logger.info("森空岛首次未完全成功，60 秒后重试一次")
    await asyncio.sleep(RETRY_DELAY)
    return await _sign_skland_once(config)


# ---------------------------------------------------------------- 镜客居

def _sign_jkju_once() -> SiteResult:
    username = os.getenv("JKJU_USER", "").strip()
    password = os.getenv("JKJU_PASSWORD", "").strip()
    is_email = os.getenv("JKJU_MODE", "").strip().lower() == "email"
    if not username or not password:
        return SiteResult(
            "镜客居", False, "no_account",
            "📅 镜客居签到\n❌ 未配置账号（JKJU_USER / JKJU_PASSWORD）",
            "在仓库 Secrets 中添加 JKJU_USER 和 JKJU_PASSWORD。",
        )

    signer = AutoSign(username, password, is_email)
    try:
        status = signer.login()
        if status == 0:
            return SiteResult(
                "镜客居", False, "captcha",
                "📅 镜客居签到\n❌ 网站要求验证码，本次未签到",
                "用浏览器登录一次 www.jkju.cc 完成验证；或等几小时冷却后，到仓库 Actions 页手动 Run workflow 重试。",
            )
        if status == -1:
            return SiteResult(
                "镜客居", False, "login_failed",
                "📅 镜客居签到\n❌ 登录失败（凭据或站点登录流程可能变更）",
                "请检查 JKJU_PASSWORD Secret 是否仍正确；如果改过站密码，需同步更新 Secret。",
            )

        signer._init_sign_page()
        _, already = signer._parse_sign_button()
        did_sign = False
        if already:
            result_line = "✅ 今日已签到"
        else:
            sign_status = signer.sign()
            if sign_status == 1:
                did_sign = True
                result_line = "✅ 签到成功"
            elif sign_status == 0:
                result_line = "✅ 今日已签到"
            else:
                return SiteResult(
                    "镜客居", False, "site_changed",
                    "📅 镜客居签到\n❌ 签到失败（响应与预期不符，站点可能改版）",
                    "站点可能改版导致脚本失效，需要更新代码。请把这封邮件正文发给维护者。",
                )

        signer._init_sign_page()
        trend = signer._get_sign_trend()
        text = f"📅 镜客居签到\n{result_line}"
        if trend:
            text += "\n" + trend
        return SiteResult("镜客居", True, "ok", text, changed=did_sign)
    except Exception as exc:
        return SiteResult(
            "镜客居", False, "error",
            f"📅 镜客居签到\n❌ 执行异常 - {type(exc).__name__}: {exc}",
            "执行过程出现异常，请把这封邮件正文发给维护者排查。",
        )


async def sign_jkju() -> SiteResult:
    result = _sign_jkju_once()
    if result.ok:
        return result
    logger.info("镜客居首次未完全成功，60 秒后重试一次")
    await asyncio.sleep(RETRY_DELAY)
    return _sign_jkju_once()


# ---------------------------------------------------------------- 邮件汇总

async def main():
    config = _load_config()

    logger.info("===== 开始森空岛签到 =====")
    skland = await sign_skland(config)
    logger.info("===== 开始镜客居签到 =====")
    jkju = await sign_jkju()

    results = [skland, jkju]
    subject = build_subject(results)
    message = build_message(results)
    logger.info("邮件标题: %s", subject)

    # 多个错峰点去重：定时触发时，若两站都是"今日已签到"且无任何异常，
    # 说明前一个错峰点已经签到并通知过了，本次跳过，避免一天收到多封邮件。
    # 手动触发（force）永远发，方便随时查看状态。
    force_send = os.getenv("FORCE_EMAIL", "").strip().lower() == "true"
    all_settled = all(r.ok and not r.changed and not r.action for r in results)
    if not force_send and all_settled:
        logger.info("两站均今日已签且无异常，跳过邮件（错峰去重）")
        return

    email_user = os.getenv("EMAIL_USER", "").strip()
    email_password = os.getenv("EMAIL_PASSWORD", "").strip()
    email_receiver = os.getenv("EMAIL_RECEIVER", "").strip() or email_user

    if not email_user or not email_password:
        logger.error("未配置 EMAIL_USER / EMAIL_PASSWORD，跳过邮件发送")
        logger.info("汇总内容:\n%s", message)
        return

    notifier = EmailNotifier(
        {
            "smtp_host": os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com"),
            "smtp_port": int(os.getenv("EMAIL_SMTP_PORT", "465")),
            "use_ssl": True,
            "username": email_user,
            "password": email_password,
            "sender": email_user,
            "receiver": email_receiver,
            "subject": subject,
        }
    )
    ok = await notifier.send(message)
    logger.info("汇总邮件发送%s", "成功" if ok else "失败（详见上方日志）")


if __name__ == "__main__":
    asyncio.run(main())
