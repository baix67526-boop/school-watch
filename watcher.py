# watcher.py
# 功能：
# 1) 读取 sources.txt（每行：学校名 URL）
# 2) 定时抓取栏目页，做“降噪指纹”对比，判断是否有更新
# 3) 读取 subscriptions.xlsx（email/schools/status），只给订阅了该学校的人发邮件（逐个单发，保护隐私）
# 4) 将最新指纹写回 state.json（并配合 Actions 提交回仓库）

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
from openpyxl import load_workbook

STATE_FILE = "state.json"
SOURCES_FILE = "sources.txt"
SUBS_FILE = "subscriptions.xlsx"

# ------------------- HTTP Session（带重试） -------------------
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

# ------------------- 邮件发送（126：SMTP SSL 465） -------------------
def send_email_to(subject: str, body: str, to_email: str):
    smtp_host = os.getenv("SMTP_HOST", "smtp.126.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")   # 你的126邮箱地址
    smtp_pass = os.getenv("SMTP_PASS")   # 你的126邮箱授权码（只放Secrets）
    if not smtp_user or not smtp_pass:
        raise RuntimeError("Missing SMTP_USER / SMTP_PASS in env")

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header(smtp_user, "utf-8")
    msg["To"] = Header(to_email, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())

# ------------------- 抓取 + 降噪指纹 -------------------
def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SchoolWatcher/1.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = SESSION.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text

def normalize_html(html: str) -> str:
    """
    降噪：去 script/style，提取纯文本；只取前400行减少页脚访问量等变化导致误报
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines[:400])

def fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

# ------------------- 文件读写 -------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_sources():
    """
    sources.txt 每行格式：学校名 URL
    例如：北京大学 https://xxx...
    """
    items = []
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split()
            if len(parts) < 2:
                # 不符合格式就跳过
                continue
            name = parts[0].strip()
            url = parts[1].strip()
            items.append({"name": name, "url": url})
    return items

# ------------------- 读取订阅Excel -------------------
def load_subscriptions():
    """
    subscriptions.xlsx 表头要求：
    email | schools | status （其余列可有可无）

    schools：用逗号分隔学校名，例如：北京大学,清华大学
    status：ACTIVE 才发，其他都不发
    """
    if not os.path.exists(SUBS_FILE):
        raise RuntimeError(f"Missing {SUBS_FILE} in repo root")

    wb = load_workbook(SUBS_FILE)
    ws = wb.active

    # 解析表头列索引
    header = {}
    for idx, cell in enumerate(ws[1], start=1):
        if cell.value:
            header[str(cell.value).strip()] = idx

    for col in ["email", "schools", "status"]:
        if col not in header:
            raise RuntimeError(f"{SUBS_FILE} 缺少表头列：{col}")

    school_to_emails = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        email = row[header["email"] - 1]
        schools = row[header["schools"] - 1]
        status = row[header["status"] - 1]

        if not email or not schools:
            continue
        if str(status).strip().upper() != "ACTIVE":
            continue

        email = str(email).strip()
        raw = str(schools).replace("，", ",")
        school_list = [s.strip() for s in raw.split(",") if s.strip()]

        for s in school_list:
            school_to_emails.setdefault(s, set()).add(email)

    return school_to_emails

# ------------------- 主流程 -------------------
def main():
    sources = load_sources()
    state = load_state()
    school_to_emails = load_subscriptions()

    # 记录更新：按学校名汇总（一个学校可能多条栏目源）
    updates_by_school = {}  # {school_name: [url1, url2, ...]}
    failures = []  # (school, url, error)

    for item in sources:
        name, url = item["name"], item["url"]
        try:
            html = fetch(url)
            norm = normalize_html(html)
            fp = fingerprint(norm)

            old_fp = state.get(url, {}).get("fp")
            if old_fp and old_fp != fp:
                updates_by_school.setdefault(name, []).append(url)

            state[url] = {"fp": fp, "ts": int(time.time())}
        except Exception as e:
            failures.append((name, url, str(e)))
            state[url] = {
                "fp": state.get(url, {}).get("fp"),
                "ts": int(time.time()),
                "error": str(e),
            }

    save_state(state)

    # 如果没有更新，就不发邮件（避免打扰）
    if not updates_by_school:
        print("No updates.")
        return

    # 按收件人汇总要发的内容（逐个单发）
    email_to_school_urls = {}  # {email: {school: [urls...]}}
    for school, urls in updates_by_school.items():
        subs = school_to_emails.get(school, set())
        for em in subs:
            email_to_school_urls.setdefault(em, {}).setdefault(school, []).extend(urls)

    # 没有人订阅这些学校也不发
    if not email_to_school_urls:
        print("Updates exist, but no subscribers matched.")
        return

    # 发送
    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    for em, school_map in email_to_school_urls.items():
        lines = [f"检测到你订阅的学校栏目有更新（{now_str}）：", ""]
        for school, urls in school_map.items():
            lines.append(f"")
            for u in urls:
                host = urlparse(u).netloc
                lines.append(f"- {host}\n  {u}")
            lines.append("")
        if failures:
            # 可选：给订阅者展示失败不展示（这里默认不展示，避免干扰）
            pass

        lines.append("提示：请以学校官网为准。若需暂停/退订，请微信联系我。")

        subject = f"【调剂信息更新提醒】{len(school_map)}所学校栏目有变化"
        body = "\n".join(lines).strip()

        send_email_to(subject, body, em)
        print(f"Sent to {em}: {len(school_map)} schools updated.")

if __name__ == "__main__":
    main()
