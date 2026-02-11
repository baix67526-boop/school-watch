import hashlib
import json
import os
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.header import Header
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

SOURCES_FILE = "sources.txt"
STATE_FILE = "state.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"}
)

TIMEOUT_SEC = 12
MAX_WORKERS = 6


@dataclass
class SourceItem:
    school: str
    url: str


def load_sources(path: str) -> list[SourceItem]:
    if not os.path.exists(path):
        raise RuntimeError(f"Missing {path} in repo root")

    items: list[SourceItem] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t", 1)
            else:
                parts = re.split(r"\s+", line, maxsplit=1)
            if len(parts) != 2:
                continue
            school, url = parts[0].strip(), parts[1].strip()
            if school and url.startswith("http"):
                items.append(SourceItem(school=school, url=url))
    return items


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"fingerprints": {}}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {"fingerprints": {}}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_xml_response(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    return ("xml" in ctype) or resp.text.lstrip().startswith("<?xml")


def normalize_content(text: str, as_xml: bool) -> str:
    """
    只提取“更像列表”的信息，降低误报：
    - XML：entry/item 的 title + link
    - HTML：a 标签的文本 + href
    """
    if as_xml:
        soup = BeautifulSoup(text, "xml")
        parts = []
        for item in soup.find_all(["item", "entry"]):
            title = (item.findtext("title") or "").strip()
            link = ""
            lk = item.find("link")
            if lk:
                link = (lk.get("href") or lk.text or "").strip()
            if title or link:
                parts.append(f"{title}||{link}")
        if parts:
            return "\n".join(parts)[:12000]
        return re.sub(r"\s+", " ", text)[:12000]

    soup = BeautifulSoup(text, "html.parser")
    links = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        title = re.sub(r"\s+", " ", (a.get_text() or "").strip())
        if not href or not title:
            continue
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if len(title) < 4:
            continue
        links.append(f"{title}||{href}")
    if links:
        return "\n".join(links)[:12000]

    clean = soup.get_text("\n")
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    return clean[:12000]


def fingerprint(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def fetch_one(item: SourceItem) -> tuple[str, str, str | None, str | None]:
    """
    returns: (school, url, fp, err)
    """
    try:
        r = SESSION.get(item.url, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        as_xml = is_xml_response(r)
        content = normalize_content(r.text, as_xml=as_xml)
        fp = fingerprint(content)
        return item.school, item.url, fp, None
    except Exception as e:
        return item.school, item.url, None, repr(e)


def send_email(subject: str, body: str, to_addr: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "0") or "0")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()

    if not (smtp_host and smtp_port and smtp_user and smtp_pass and to_addr):
        raise RuntimeError("Missing SMTP settings or MAIL_TO")

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header(smtp_user, "utf-8")
    msg["To"] = Header(to_addr, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")

    # 465/994 走 SSL；其余尝试 STARTTLS
    if smtp_port in (465, 994):
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=25)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=25)
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except Exception:
            pass

    try:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_addr], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


def main():
    mail_to = (os.getenv("MAIL_TO", "") or "").strip()
    if not mail_to:
        raise RuntimeError("MAIL_TO is empty")

    sources = load_sources(SOURCES_FILE)
    state = load_state(STATE_FILE)
    fps: dict = state.get("fingerprints", {})

    updates = []   # (school, url)
    failures = []  # (school, url, err)

    # 并发抓取
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(fetch_one, it) for it in sources]
        for fu in as_completed(futures):
            school, url, fp, err = fu.result()
            if fp is None:
                failures.append((school, url, err))
                continue

            old = fps.get(url)
            if old and old != fp:
                updates.append((school, url))
            fps[url] = fp

    # 保存 state（无论是否更新都保存，便于下次对比）
    state["fingerprints"] = fps
    save_state(STATE_FILE, state)

    if not updates:
        print(f"No updates. sources={len(sources)} failures={len(failures)}")
        return

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    subject = f"【学校官网更新】{len(updates)}条｜{now_str}"

    lines = []
    lines.append(f"时间：{now_str}")
    lines.append(f"监控源：{len(sources)}")
    lines.append(f"更新：{len(updates)}")
    lines.append(f"失败：{len(failures)}")
    lines.append("")
    lines.append("=== 更新列表 ===")
    for school, url in sorted(updates, key=lambda x: x[0]):
        lines.append(f"- {school}：{url}")

    if failures:
        lines.append("")
        lines.append("=== 抓取失败（下次自动重试）===")
        for school, url, err in failures[:20]:
            lines.append(f"- {school}：{url}")
            lines.append(f"  err: {err}")

    body = "\n".join(lines)

    send_email(subject, body, mail_to)
    print(f"Sent update email to {mail_to}. updates={len(updates)} failures={len(failures)}")


if __name__ == "__main__":
    main()
