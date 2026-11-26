"""镜客居 (www.jkju.cc) 每日签到脚本 - GitHub Actions 版。

凭据来源优先级：命令行参数 > 环境变量 (JKJU_USER / JKJU_PASSWORD / JKJU_MODE)。
环境变量方式用于 GitHub Actions，凭据存放在仓库 Secrets 中，不会明文出现在代码里。
"""

import argparse
import hashlib
import os
from time import sleep

import requests
from bs4 import BeautifulSoup


class AutoSign:
    BASE_URL = "https://www.jkju.cc/"
    LOGIN_PAGE = BASE_URL + "member.php"
    LOGIN_URL = BASE_URL + "member.php"
    SIGN_PAGE_URL = BASE_URL + "plugin.php?id=zqlj_sign"

    LOGIN_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        ),
        "Origin": BASE_URL,
        "Referer": LOGIN_PAGE + "?mod=logging&action=login",
    }

    SIGN_HEADERS = {
        "User-Agent": LOGIN_HEADERS["User-Agent"],
        "Referer": SIGN_PAGE_URL,
    }

    LOGIN_FORM_TEMPLATE = {
        "referer": BASE_URL + "index.php",
        "questionid": 0,
        "answer": "",
    }

    LOGIN_PARAMS_TEMPLATE = {
        "mod": "logging",
        "action": "login",
        "loginsubmit": "yes",
        "inajax": 1,
    }

    def __init__(self, username: str, password: str, is_email: bool = False):
        self.session = requests.Session()
        self.username = username
        self.password = password
        self.login_field = "email" if is_email else "username"

        self.login_form_data = self.LOGIN_FORM_TEMPLATE.copy()
        self.login_form_data.update(
            {"username": username, "password": password, "loginfield": self.login_field}
        )

        self.sign_url: str | None = None
        self.sign_page_html: str | None = None
        self.messages: list[str] = [f"签到任务: 镜客居", f"登录账号: {username}"]

    # -------------------------
    # HTTP 请求封装
    # -------------------------
    def _get(self, url: str, **kwargs) -> requests.Response:
        resp = self.session.get(url, timeout=20, **kwargs)
        if resp.status_code == 403:
            self.session.cookies.clear_expired_cookies()
            resp = self.session.get(url, timeout=20, **kwargs)
        return resp

    def _post(self, url: str, data=None, params=None, headers=None) -> requests.Response:
        resp = self.session.post(
            url, data=data, params=params, headers=headers, timeout=20
        )
        if resp.status_code == 403:
            self.session.cookies.clear_expired_cookies()
            resp = self.session.post(
                url, data=data, params=params, headers=headers, timeout=20
            )
        return resp

    # -------------------------
    # 登录相关
    # -------------------------
    def _load_login_page(self) -> str:
        for _ in range(3):
            resp = self._get(
                self.LOGIN_PAGE, params={"mod": "logging", "action": "login"}
            )
            if "document.location.reload" not in resp.text:
                return resp.text
            sleep(1)
        raise RuntimeError("登录页面加载失败或一直刷新")

    def _parse_login_form(self, html: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        form_tag = soup.find("form", {"name": "login"})
        if not form_tag:
            raise RuntimeError("登录页结构变化：未找到 login 表单")
        formhash_input = form_tag.find("input", {"name": "formhash", "type": "hidden"})
        if not formhash_input or not formhash_input.get("value"):
            raise RuntimeError("登录页结构变化：未找到 formhash")
        action = form_tag.get("action") or ""
        if "loginhash=" not in action:
            raise RuntimeError("登录页结构变化：表单 action 中未找到 loginhash")
        return formhash_input["value"], action.split("&")[-1].split("=")[-1]

    def _get_login_hash(self) -> tuple[str, str]:
        html = self._load_login_page()
        return self._parse_login_form(html)

    def login(self) -> int:
        """返回 1=成功, 0=需要验证码, -1=失败。"""
        formhash, loginhash = self._get_login_hash()
        self.login_form_data["formhash"] = formhash
        login_params = self.LOGIN_PARAMS_TEMPLATE.copy()
        login_params["loginhash"] = loginhash

        resp = self._post(
            self.LOGIN_URL,
            data=self.login_form_data,
            params=login_params,
            headers=self.LOGIN_HEADERS,
        )
        # 修正：站点实际返回「请输入验证码后继续登录」，原脚本少匹配一个「后」字
        if "验证码" in resp.text:
            return 0
        if "欢迎您回来" in resp.text:
            return 1
        return -1

    # -------------------------
    # 签到相关
    # -------------------------
    def _init_sign_page(self):
        resp = self._get(self.SIGN_PAGE_URL)
        self.sign_page_html = resp.text

    def _parse_sign_button(self) -> tuple[str, bool]:
        soup = BeautifulSoup(self.sign_page_html, "html.parser")
        sign_div = soup.find("div", class_="bm signbtn cl")
        if not sign_div:
            raise RuntimeError("签到页结构变化：未找到签到按钮区域")
        sign_btn = sign_div.find("a")
        if not sign_btn or not sign_btn.get("href"):
            raise RuntimeError("签到页结构变化：未找到签到链接")
        href = sign_btn.get("href")
        self.sign_url = (
            href if href.startswith("http") else self.BASE_URL + href.lstrip("/")
        )
        return self.sign_url, "今日已打卡" in sign_btn.text

    def _get_sign_trend(self) -> str:
        soup = BeautifulSoup(self.sign_page_html, "lxml")
        trend_lis = soup.select(
            "#wp > div.ct2.cl > div.sd > div:nth-of-type(3) > div.bm_c > ul > li"
        )
        return "\n".join(li.text for li in trend_lis)

    def sign(self) -> int:
        """返回 1=签到成功, 0=今日已签到, -1=失败。"""
        if not self.sign_url:
            raise RuntimeError("签到 URL 未初始化")
        resp = self._get(self.sign_url, headers=self.SIGN_HEADERS)
        if "恭喜您，打卡成功" in resp.text:
            return 1
        if "已经打过卡" in resp.text or "请勿重复" in resp.text:
            return 0
        return -1

    # -------------------------
    # 执行入口
    # -------------------------
    def start(self):
        try:
            status = self.login()
            if status == 0:
                print("本次未签到：网站要求验证码（需手动登录一次或等待冷却）")
                return
            if status == -1:
                print("本次未签到：登录失败（请检查凭据或站点登录流程是否变更）")
                return

            self._init_sign_page()
            _, already_signed = self._parse_sign_button()
            if already_signed:
                self.messages.append("执行结果: 今日已签到")
            else:
                sign_status = self.sign()
                if sign_status == 1:
                    self.messages.append("执行结果: 签到成功")
                elif sign_status == 0:
                    self.messages.append("执行结果: 今日已签到")
                else:
                    self.messages.append("执行结果: 签到失败（响应与预期不符，站点可能改版）")

            self._init_sign_page()
            self.messages.append(self._get_sign_trend())
            print("\n".join(self.messages))
        except Exception as exc:
            print(f"本次未签到：执行异常 - {type(exc).__name__}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="镜客居每日签到")
    parser.add_argument("-u", "--user", help="用户名（也可用环境变量 JKJU_USER）")
    parser.add_argument("-p", "--password", help="密码（也可用环境变量 JKJU_PASSWORD）")
    parser.add_argument(
        "-m", "--mode", help="登录模式: username | email（也可用环境变量 JKJU_MODE）"
    )
    args = parser.parse_args()

    username = args.user or os.environ.get("JKJU_USER", "")
    password = args.password or os.environ.get("JKJU_PASSWORD", "")
    mode = args.mode or os.environ.get("JKJU_MODE", "")

    if not username or not password:
        print("缺少凭据：请设置 JKJU_USER 和 JKJU_PASSWORD 环境变量，或使用 -u / -p 参数")
        raise SystemExit(1)

    is_email = mode.strip().lower() == "email"
    AutoSign(username, password, is_email).start()


if __name__ == "__main__":
    main()
