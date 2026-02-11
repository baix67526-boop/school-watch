import os
import json
import time
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STATE_FILE = "state.json"
SOURCES_FILE = "sources.txt"

def make_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

SESSION = make_session()

def send_email(subject: str, body: str):
    smtp_host = os.getenv("SMTP_HOST", "smtp.126.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")  # 126邮箱授权码（放GitHub Secrets）
    mail_to = os.getenv("MAIL_TO")

    if not all([smtp_user, smtp_pass, mail_to]):
        raise RuntimeError("Missing env vars: SMTP_USER/SMTP_PASS/MAIL_TO")

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header(smtp_user, "utf-8")
    msg["To"] = Header(mail_to, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [mail_to], msg.as_string())

def normalize_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    # 取前400行，减少页脚访问量/版权等导致的误报
    return "\n".join(lines[:400])

def fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SchoolWatcher/1.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = SESSION.get(url, headers=headers, timeout=25)
    # 即使遇到 403/404，也让上层记录 error
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f.readlines()]
    items = []
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        # 允许 "学校名 URL" 或仅URL
        parts = ln.split()
        if len(parts) == 1:
            items.append({"name": "", "url": parts[0]})
        else:
            items.append({"name": parts[0], "url": parts[1]})
    return items

def main():
    sources = load_sources()
    state = load_state()

    updates = []
    failures = []

    for item in sources:
        name, url = item["name"], item["url"]
        try:
            html = fetch(url)
            norm = normalize_html(html)
            fp = fingerprint(norm)

            old_fp = state.get(url, {}).get("fp")
            if old_fp and old_fp != fp:
                updates.append((name, url))

            state[url] = {"fp": fp, "ts": int(time.time())}
        except Exception as e:
            failures.append((name, url, str(e)))
            # 保留旧fp，避免失败导致反复“首次”触发
            state[url] = {
                "fp": state.get(url, {}).get("fp"),
                "ts": int(time.time()),
                "error": str(e),
            }

    save_state(state)

    if updates or failures:
        lines = []
        if updates:
            lines.append(f"检测到栏目页更新：{len(updates)} 个")
            lines.append("")
            for name, url in updates:
                host = urlparse(url).netloc
                prefix = f"{name} | " if name else ""
                lines.append(f"- {prefix}{host}\n  {url}")
            lines.append("")

        if failures:
            lines.append(f"抓取失败：{len(failures)} 个（可能是网站限制/临时故障）")
            lines.append("")
            for name, url, err in failures[:10]:
                prefix = f"{name} | " if name else ""
                lines.append(f"- {prefix}{url}\n  错误：{err}")
            if len(failures) > 10:
                lines.append(f"... 另有 {len(failures)-10} 个失败未展开")
            lines.append("")

        subject = f"【官网监控提醒】更新{len(updates)} / 失败{len(failures)}"
        body = "\n".join(lines).strip()
        send_email(subject, body)
        print(body)
    else:
        print("No updates, no failures.")

if __name__ == "__main__":
    main()
