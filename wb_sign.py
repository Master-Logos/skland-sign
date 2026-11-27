"""WorkBuddy 每日签到 + 积分概览。

供 daily_sign.py 编排调用，和森空岛、镜客居一起汇总成一封邮件。

凭据来源（按优先级）：
1. state/wb_session.enc —— 上次运行续期后加密保存的凭据（WB_STATE_KEY 解密）
2. 环境变量 WB_SESSION —— 首次部署时从本机看板的 data/session.json 复制过来

WorkBuddy 每次续期都会轮换 refresh token，所以运行结束后必须把新凭据写回去，
否则 refresh token 30 天后就断了。写回的是 AES-GCM 加密后的密文，
即使仓库是公开的也不会泄露明文凭据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_BASE = "https://copilot.tencent.com/v2/plugin"
CLIENT_VERSION = "2.184.0"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
FALLBACK_ORIGIN = "https://www.workbuddy.cn"
ALLOWED_HOSTS = {"codebuddy.cn", "www.codebuddy.cn", "workbuddy.cn", "www.workbuddy.cn"}

CN_TZ = timezone(timedelta(hours=8))
DAY = 86400.0
STATE_PATH = Path(__file__).resolve().parent / "state" / "wb_session.enc"
TIMEOUT = 20

PLUGIN_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": f"CLI/{CLIENT_VERSION} CodeBuddy/{CLIENT_VERSION}",
    "x-product": "SaaS",
    "x-ide-type": "CLI",
    "x-ide-name": "CodeBuddy",
    "x-ide-version": CLIENT_VERSION,
}


@dataclass
class WbSignResult:
    """与 daily_sign.SiteResult 字段一致，便于直接转换。"""

    name: str = "WorkBuddy"
    ok: bool = False
    status: str = "error"
    text: str = ""
    action: str = ""
    changed: bool = False


# ---------------------------------------------------------------- 时间与格式化


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_cn_time(value) -> int | None:
    """接口里的时间串统一是北京时间，转成毫秒时间戳。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e12 else value * 1000)
    text = str(value).strip()
    if text.isdigit():
        return _parse_cn_time(int(text))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
            return int(dt.replace(tzinfo=CN_TZ).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _fmt_dt(ts: int | None, now: int | None = None) -> str:
    """11/30 23:59；跨年时带年份。"""
    if not ts:
        return "—"
    d = datetime.fromtimestamp(ts / 1000, CN_TZ)
    n = datetime.fromtimestamp((now or _now_ms()) / 1000, CN_TZ)
    prefix = "" if d.year == n.year else f"{d.year}/"
    clock = f" {d.hour:02d}:{d.minute:02d}" if (d.hour or d.minute) else ""
    return f"{prefix}{d.month}/{d.day}{clock}"


def _days_left(ts: int | None, now: int | None = None) -> int | None:
    """还有几个「整天」。时间戳是毫秒，DAY 是秒，别混。"""
    if not ts:
        return None
    return math.floor((ts - (now or _now_ms())) / (DAY * 1000))


def _days_text(days: int | None) -> str:
    if days is None:
        return "—"
    if days <= 0:
        return "不到 1 天"
    return f"{days} 天后"


def _cn_today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 状态加密


def _state_key() -> bytes | None:
    raw = os.getenv("WB_STATE_KEY", "").strip()
    return hashlib.sha256(raw.encode("utf-8")).digest() if raw else None


def _encrypt(payload: dict) -> bytes | None:
    key = _state_key()
    if not key:
        return None
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes

    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body, tag = cipher.encrypt_and_digest(data)
    return b"WBS1" + nonce + tag + body


def _decrypt(blob: bytes) -> dict | None:
    key = _state_key()
    if not key or not blob.startswith(b"WBS1"):
        return None
    from Crypto.Cipher import AES

    nonce, tag, body = blob[4:16], blob[16:32], blob[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return json.loads(cipher.decrypt_and_verify(body, tag).decode("utf-8"))


def load_state() -> dict:
    """优先读加密状态，其次读 WB_SESSION Secret。"""
    if STATE_PATH.exists():
        try:
            state = _decrypt(STATE_PATH.read_bytes())
            if state and state.get("auth", {}).get("refreshToken"):
                return state
        except Exception as exc:  # 密钥换过或文件损坏，退回 Secret
            print(f"[WorkBuddy] 状态文件无法解密（{type(exc).__name__}），改用 WB_SESSION")

    raw = os.getenv("WB_SESSION", "").strip()
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        try:  # 也接受 base64，避免多行 JSON 在 Secret 里被折腾
            state = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            print("[WorkBuddy] WB_SESSION 既不是 JSON 也不是 base64")
            return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict) -> bool:
    blob = _encrypt(state)
    if not blob:
        print("[WorkBuddy] 未配置 WB_STATE_KEY，跳过凭据回写（下次仍用 WB_SESSION）")
        return False
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_bytes(blob)
    print(f"[WorkBuddy] 已加密回写凭据 → {STATE_PATH.name}")
    return True


# ---------------------------------------------------------------- HTTP


def _http_json(url: str, headers: dict, body: dict | str | None, action: str) -> tuple[int, dict | None]:
    data = None
    if body is not None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        status = exc.code
    except Exception as exc:
        raise RuntimeError(f"{action}请求失败：{type(exc).__name__} {exc}") from exc
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise RuntimeError(f"{action}返回了无法解析的数据（HTTP {status}）") from None


def _calc_expiry(auth: dict) -> dict:
    out = dict(auth)
    now = _now_ms()
    if not out.get("expiresAt") and isinstance(out.get("expiresIn"), (int, float)):
        out["expiresAt"] = now + int(out["expiresIn"] * 1000)
    if not out.get("refreshExpiresAt") and isinstance(out.get("refreshExpiresIn"), (int, float)):
        out["refreshExpiresAt"] = now + int(out["refreshExpiresIn"] * 1000)
    return out


def _refresh(state: dict) -> dict:
    auth = state.get("auth") or {}
    if not auth.get("refreshToken"):
        raise RuntimeError("缺少 refresh token")
    account = state.get("account") or {}
    headers = dict(PLUGIN_HEADERS)
    enterprise_id = account.get("enterpriseId")
    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(enterprise_id)
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    headers["X-Refresh-Token"] = auth["refreshToken"]
    headers["X-Auth-Refresh-Source"] = "plugin"

    status, payload = _http_json(f"{PLUGIN_BASE}/auth/token/refresh", headers, "{}", "令牌续期")
    if status >= 400 or (payload or {}).get("code") not in (0, None):
        msg = (payload or {}).get("message") or (payload or {}).get("msg") or status
        raise RuntimeError(f"令牌续期失败（{msg}）")

    fresh = _calc_expiry((payload or {}).get("data") or {})
    merged = {**auth, **fresh}
    merged["refreshToken"] = fresh.get("refreshToken") or auth["refreshToken"]
    if not merged.get("accessToken"):
        raise RuntimeError("续期接口没有返回访问令牌")
    return {**state, "auth": merged}


def _billing_host(state: dict) -> str:
    raw = (state.get("auth") or {}).get("domain") or ""
    if raw:
        text = raw if raw.startswith("http") else f"https://{raw}"
        try:
            host = urllib.request.urlparse(text).hostname or ""
        except Exception:
            host = ""
        if host.lower() in ALLOWED_HOSTS:
            return f"https://{host.lower()}"
    return FALLBACK_ORIGIN


def _billing_headers(state: dict, host: str) -> dict:
    auth = state.get("auth") or {}
    account = state.get("account") or {}
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-client-platform": "web",
        "origin": host,
        "referer": f"{host}/profile/plans-usage",
        "authorization": f"Bearer {auth['accessToken'].strip()}",
        "user-agent": BROWSER_UA,
    }
    if auth.get("domain"):
        headers["x-domain"] = str(auth["domain"]).strip()
    if account.get("userId"):
        headers["x-user-id"] = str(account["userId"]).strip()
    if account.get("enterpriseId"):
        headers["x-enterprise-id"] = str(account["enterpriseId"])
        headers["x-tenant-id"] = str(account["enterpriseId"])
    return headers


def _post_billing(state: dict, path: str, body: dict, action: str) -> dict:
    host = _billing_host(state)
    status, payload = _http_json(f"{host}{path}", _billing_headers(state, host), body, action)
    if status >= 400:
        raise RuntimeError(f"{action} HTTP {status}")
    if payload and payload.get("code") not in (0, None):
        raise RuntimeError(f"{action}失败（{payload.get('msg') or payload.get('message') or payload.get('code')}）")
    return payload or {}


# ---------------------------------------------------------------- 签到


def _checkin(state: dict) -> dict:
    host = _billing_host(state)
    headers = _billing_headers(state, host)
    for path in ("/v2/billing/meter/daily-checkin", "/billing/meter/daily-checkin"):
        try:
            status, payload = _http_json(f"{host}{path}", headers, "{}", "签到")
        except RuntimeError as exc:
            return {"ok": False, "status": "error", "message": str(exc), "reward": None}
        if status == 404:  # 接口路径换版本，试下一个
            continue

        payload = payload or {}
        code = payload.get("code")
        message = str(payload.get("message") or payload.get("msg") or "")
        inactive = any(k in message for k in ("未开启", "未开始", "未开放", "已过期")) or (
            "活动" in message and any(k in message for k in ("结束", "关闭", "暂停"))
        )
        unsupported = "企业账号" in message or ("不支持" in message and "签到" in message)
        already = status in (200, 400) and code == 10001 and not inactive and any(
            k in message for k in ("已签到", "已领取", "重复签到")
        )
        data = payload.get("data") or {}
        reward_raw = next(
            (data.get(k) for k in ("reward_credit", "rewardCredits", "credits", "credit") if data.get(k) is not None),
            None,
        )
        try:
            reward = float(reward_raw) if reward_raw is not None else None
        except (TypeError, ValueError):
            reward = None

        if inactive or unsupported:
            return {
                "ok": False,
                "status": "inactive",
                "message": "签到活动暂不可用" if inactive else "企业账号不支持签到",
                "reward": None,
            }
        if (status == 200 and code in (0, None)) or already:
            return {
                "ok": True,
                "status": "already" if already else "ok",
                "message": "今日已签到" if already else "签到成功",
                "reward": None if already else reward,
            }
        if status in (401, 403):
            return {"ok": False, "status": "login_failed", "message": "登录已失效", "reward": None}
        return {
            "ok": False,
            "status": "error",
            "message": f"签到失败（HTTP {status}，业务码 {code if code is not None else '未知'}）",
            "reward": None,
        }
    return {"ok": False, "status": "error", "message": "签到接口不可用", "reward": None}


# ---------------------------------------------------------------- 积分


def _attr(account: dict, key: str) -> str:
    for item in account.get("AccountAttributes") or []:
        if isinstance(item, dict) and item.get("Key") == key:
            return str(item.get("Value") or "")
    return ""


def _num(*values) -> float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _to_batch(account: dict) -> dict:
    cycle_end = _parse_cn_time(account.get("CycleEndTime"))
    deduct_end = _parse_cn_time(account.get("DeductionEndTime"))
    # 周期配额（套餐月度额度）：CapacityType=4，或「会员到期」远晚于「本周期结束」
    is_cycle = account.get("CapacityType") == 4 or bool(
        cycle_end and deduct_end and deduct_end - cycle_end > 60 * DAY * 1000
    )
    size = _num(
        account.get("CycleCapacitySizePrecise"),
        account.get("CapacitySizePrecise"),
        account.get("CycleCapacitySize"),
        account.get("CapacitySize"),
    )
    remain = _num(
        account.get("CycleCapacityRemainPrecise"),
        account.get("CapacityRemainPrecise"),
        account.get("CycleCapacityRemain"),
        account.get("CapacityRemain"),
    )
    name = _attr(account, "grantReason") or str(account.get("PackageName") or "积分包")
    return {
        "mode": "cycle" if is_cycle else "grant",
        "name": name,
        "size": round(size, 2),
        "remain": round(max(0.0, remain), 2),
        "refresh_at": cycle_end if is_cycle else None,
        "expire_at": deduct_end or cycle_end,
    }


def _credits(state: dict) -> dict:
    now = _now_ms()
    end = datetime.fromtimestamp((now + 101 * 365 * DAY * 1000) / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    begin = datetime.fromtimestamp(now / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    payload = _post_billing(
        state,
        "/v2/billing/meter/get-user-resource",
        {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": begin,
            "PackageEndTimeRangeEnd": end,
        },
        "积分接口",
    )
    accounts = []
    for candidate in (
        (payload.get("data") or {}).get("Response", {}).get("Data", {}).get("Accounts"),
        ((payload.get("data") or {}).get("data") or {}).get("Response", {}).get("Data", {}).get("Accounts"),
        (payload.get("data") or {}).get("accounts"),
    ):
        if isinstance(candidate, list):
            accounts = candidate
            break

    batches = [b for b in (_to_batch(a) for a in accounts) if b["remain"] > 0 or b["size"] > 0]
    cycle = [b for b in batches if b["mode"] == "cycle"]
    grants = sorted([b for b in batches if b["mode"] == "grant"], key=lambda b: b["expire_at"] or float("inf"))
    total = round(sum(b["remain"] for b in batches), 2)
    return {
        "total": total,
        "cycle_remain": round(sum(b["remain"] for b in cycle), 2),
        "grant_remain": round(sum(b["remain"] for b in grants), 2),
        "cycle": cycle,
        "grants": grants,
        "nearest_expire": next((b["expire_at"] for b in grants if b["expire_at"]), None),
        "expiring_30d": round(
            sum(b["remain"] for b in grants if b["expire_at"] and b["expire_at"] - now <= 30 * DAY * 1000), 2
        ),
    }


def _fmt_amount(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value):,}"


# ---------------------------------------------------------------- 对外入口


def sign_workbuddy_sync() -> WbSignResult:
    state = load_state()
    if not state.get("auth", {}).get("refreshToken"):
        return WbSignResult(
            ok=False,
            status="no_account",
            text="📅 WorkBuddy 签到\n❌ 未配置登录凭据（WB_SESSION / state/wb_session.enc）",
            action=(
                "在仓库 Settings → Secrets and variables → Actions 添加 WB_SESSION，"
                "内容为本机看板 data/session.json 的完整 JSON；同时添加 WB_STATE_KEY（任意长随机字符串）。"
            ),
        )

    try:
        state = _refresh(state)
    except Exception as exc:
        return WbSignResult(
            ok=False,
            status="login_failed",
            text=f"📅 WorkBuddy 签到\n❌ 登录凭据续期失败 - {exc}",
            action=(
                "WorkBuddy 登录已失效。在笔记本上运行 buddy-credits 看板并重新登录，"
                "把新的 data/session.json 覆盖到 WB_SESSION Secret，删除仓库里的 state/wb_session.enc 后重跑。"
            ),
        )

    tried = 0
    check = _checkin(state)
    while not check["ok"] and check["status"] == "error" and tried < 1:
        tried += 1
        print("[WorkBuddy] 签到失败，20 秒后重试一次")
        time.sleep(20)
        check = _checkin(state)

    credits = None
    credit_error = None
    try:
        credits = _credits(state)
    except Exception as exc:
        credit_error = str(exc)

    today = _cn_today()
    reported_today = state.get("last_report_date") == today
    state["last_report_date"] = today
    if check["ok"] and check["status"] == "ok":
        state["last_signed_date"] = today
    save_state(state)

    lines = ["📅 WorkBuddy 签到"]
    if check["ok"]:
        if check["status"] == "already":
            lines.append("✅ 今日已签到")
        elif check.get("reward"):
            lines.append(f"✅ 签到成功，+{_fmt_amount(check['reward'])} 分")
        else:
            lines.append("✅ 签到成功")
    else:
        lines.append(f"❌ {check['message']}")

    action = ""
    now = _now_ms()
    if credits:
        lines.append(
            f"💎 剩余 {_fmt_amount(credits['total'])} 分"
            f"（套餐 {_fmt_amount(credits['cycle_remain'])} · 奖励 {_fmt_amount(credits['grant_remain'])}）"
        )
        for batch in credits["cycle"]:
            days = _days_left(batch["refresh_at"], now)
            if days is None or days < 0:
                continue
            lines.append(
                f"🔄 套餐 {_fmt_dt(batch['refresh_at'], now)} 刷新，"
                f"届时 {_fmt_amount(batch['remain'])} 分清零（{_days_text(days)}）"
            )
            if days <= 3 and batch["remain"] > 0:
                action = (
                    f"套餐积分还剩 {_fmt_amount(batch['remain'])} 分，"
                    f"{_days_text(days)}（{_fmt_dt(batch['refresh_at'], now)}）刷新即清零，建议尽快用掉。"
                )
            break
        nearest = credits["nearest_expire"]
        if nearest:
            days = _days_left(nearest, now)
            remain_at_risk = next(
                (b["remain"] for b in credits["grants"] if b["expire_at"] == nearest), 0
            )
            lines.append(
                f"⏳ 最近到期 {_fmt_dt(nearest, now)}，"
                f"{_fmt_amount(remain_at_risk)} 分（{_days_text(days)}）"
            )
    elif credit_error:
        lines.append(f"⚠️ 积分查询失败：{credit_error}")

    if not check["ok"] and check["status"] == "login_failed":
        action = (
            "WorkBuddy 登录已失效。在笔记本上运行 buddy-credits 看板并重新登录，"
            "把新的 data/session.json 覆盖到 WB_SESSION Secret，删除 state/wb_session.enc 后重跑。"
        )
    elif not check["ok"] and check["status"] not in ("inactive",) and not action:
        action = "WorkBuddy 签到出现异常，请查看 Actions 日志；若持续失败可能需要更新 wb_sign.py。"

    status = "ok"
    if check["status"] == "login_failed":
        status = "login_failed"
    elif not check["ok"] and check["status"] not in ("inactive",):
        status = "error"
    elif not credits and credit_error:
        status = "ok"  # 签到成功就算成功，积分缺失不影响

    return WbSignResult(
        ok=check["ok"] or (credits is not None),
        status=status,
        text="\n".join(lines),
        action=action,
        # 当天第一次汇报就发邮件；后续重复触发（错峰点）不再发，避免一天多封
        changed=not reported_today,
    )


async def sign_workbuddy() -> WbSignResult:
    """给 daily_sign.py 用的异步包装（HTTP 是同步的，丢到线程池）。"""
    import asyncio

    return await asyncio.to_thread(sign_workbuddy_sync)
