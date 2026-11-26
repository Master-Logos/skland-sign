# daily_sign.py
"""统一签到编排：依次完成森空岛与镜客居签到，汇总成一封邮件。

复用仓库里现有的 skland_api.py / notifier.py / jkju/main.py，不改动它们的签到逻辑。
每个站点内部最多尝试 2 次（间隔 60 秒），任一成功或"已签到"即视为完成。
"""

import asyncio
import json
import logging
import os
from time import sleep

import yaml

from notifier import EmailNotifier
from skland_api import SklandAPI
from jkju.main import AutoSign

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DailySign")

RETRY_DELAY = 60  # 秒


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


async def _sign_skland_once(config: dict) -> tuple[str, bool]:
    """执行一次森空岛签到，返回 (结果文本, 是否成功)。"""
    users = _parse_skland_users(config)
    if not users:
        return "❌ 未配置森空岛账号（SKLAND_TOKEN(S) 或 config.yaml）", False

    lines = ["📅 森空岛签到姬", ""]
    all_ok = True
    api = SklandAPI(max_retries=3)
    try:
        for idx, user in enumerate(users, 1):
            nickname = user.get("nickname", "未知用户")
            token = user.get("token")
            lines.append(f"🌈 No.{idx}({nickname}):")
            if not token:
                lines.append("❌ 账号配置错误: 缺少Token")
                lines.append("")
                all_ok = False
                continue
            try:
                results, _official = await api.do_full_sign_in(token)
                if not results:
                    lines.append("❌ 未找到绑定角色")
                    all_ok = False
                for r in results:
                    already = not r.success and any(
                        k in r.error for k in ["已签到", "重复", "already"]
                    )
                    if r.success:
                        detail = f" ({', '.join(r.awards)})" if r.awards else ""
                        line = f"✅ {r.game}: 成功{detail}"
                    elif already:
                        line = f"✅ {r.game}: 已签"
                    else:
                        line = f"❌ {r.game}: 失败 ({r.error})"
                        all_ok = False
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
    return "\n".join(lines), all_ok


async def sign_skland(config: dict) -> str:
    text, ok = await _sign_skland_once(config)
    if ok:
        return text
    logger.info("森空岛首次未完全成功，60 秒后重试一次")
    await asyncio.sleep(RETRY_DELAY)
    text2, _ = await _sign_skland_once(config)
    return text2


# ---------------------------------------------------------------- 镜客居

def _sign_jkju_once() -> tuple[str, bool]:
    username = os.getenv("JKJU_USER", "").strip()
    password = os.getenv("JKJU_PASSWORD", "").strip()
    is_email = os.getenv("JKJU_MODE", "").strip().lower() == "email"
    if not username or not password:
        return "❌ 未配置镜客居账号（JKJU_USER / JKJU_PASSWORD）", False

    signer = AutoSign(username, password, is_email)
    try:
        status = signer.login()
        if status == 0:
            return "❌ 本次未签到：网站要求验证码（需手动登录一次或等待冷却）", False
        if status == -1:
            return "❌ 本次未签到：登录失败（凭据或站点登录流程可能变更）", False

        signer._init_sign_page()
        _, already = signer._parse_sign_button()
        if already:
            result_line = "✅ 今日已签到"
        else:
            sign_status = signer.sign()
            if sign_status == 1:
                result_line = "✅ 签到成功"
            elif sign_status == 0:
                result_line = "✅ 今日已签到"
            else:
                result_line = "❌ 签到失败（响应与预期不符，站点可能改版）"
                return f"📅 镜客居签到\n{result_line}", False

        signer._init_sign_page()
        trend = signer._get_sign_trend()
        text = f"📅 镜客居签到\n{result_line}"
        if trend:
            text += "\n" + trend
        return text, True
    except Exception as exc:
        return f"❌ 执行异常 - {type(exc).__name__}: {exc}", False


async def sign_jkju() -> str:
    text, ok = _sign_jkju_once()
    if ok:
        return text
    logger.info("镜客居首次未完全成功，60 秒后重试一次")
    await asyncio.sleep(RETRY_DELAY)
    text2, _ = _sign_jkju_once()
    return text2


# ---------------------------------------------------------------- 邮件汇总

async def main():
    config = _load_config()

    logger.info("===== 开始森空岛签到 =====")
    skland_text = await sign_skland(config)
    logger.info("===== 开始镜客居签到 =====")
    jkju_text = await sign_jkju()

    message = (
        "📅 每日签到汇总\n"
        "════════════════\n\n"
        f"{skland_text}\n\n"
        "────────────────\n\n"
        f"{jkju_text}"
    )

    email_user = os.getenv("EMAIL_USER", "").strip()
    email_password = os.getenv("EMAIL_PASSWORD", "").strip()
    email_receiver = os.getenv("EMAIL_RECEIVER", "").strip() or email_user

    if not email_user or not email_password:
        logger.error("未配置 EMAIL_USER / EMAIL_PASSWORD，跳过邮件发送")
        logger.info("汇总内容:\n%s", message)
        return

    notifier = EmailNotifier(
        {
            "smtp_host": os.getenv("EMAIL_SMTP_HOST", "smtp.163.com"),
            "smtp_port": int(os.getenv("EMAIL_SMTP_PORT", "465")),
            "use_ssl": True,
            "username": email_user,
            "password": email_password,
            "sender": email_user,
            "receiver": email_receiver,
            "subject": "每日签到汇总",
        }
    )
    ok = await notifier.send(message)
    logger.info("汇总邮件发送%s", "成功" if ok else "失败（详见上方日志）")


if __name__ == "__main__":
    asyncio.run(main())
