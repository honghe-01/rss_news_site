# -*- coding: utf-8 -*-
"""
fetch_news.py（给 GitHub Pages 用的版本）
========================================
作用：
- 抓取 BBC + NHK RSS
- 访问新闻链接，提取“第一段摘要”（优先 meta description，其次第一个 <p>）
- 生成 docs/news.json，供网页读取展示
- （可选）如果环境安装了 argostranslate 并且有 en->zh 模型，则自动翻译英文标题/摘要到中文
  - 日文 ja->zh 可能无离线模型：会写入“占位”文字，满足你要的“中文翻译占位”

注意：
- GitHub Actions 每天跑一次这个脚本，然后把 docs/news.json commit 回仓库
"""

import json
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# ====== 1) 你可以改的配置 ======
RSS = [
    ("BBC News", "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml"),
    ("NHKニュース", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
]

OUT = "docs/news.json"

# 每个源最多取多少条（防止太慢/太多）
MAX_PER_FEED = 30

# 抓网页摘要：每篇之间暂停一下，避免请求太密
ARTICLE_SLEEP = 0.25

# 超时与重试
TIMEOUT = 12
RETRIES = 2
RETRY_SLEEP = 1


# ====== 2) 网络请求（带重试） ======
def get_with_retry(url: str):
    headers = {"User-Agent": "rss-news-site/2.0"}
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=headers)
            r.raise_for_status()
            return r
        except Exception:
            if attempt < RETRIES:
                time.sleep(RETRY_SLEEP)
            else:
                return None


# ====== 3) 从网页提取“第一段摘要” ======
def first_paragraph(url: str) -> str:
    if not url:
        return ""
    r = get_with_retry(url)
    if r is None:
        return ""

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        # 优先用 meta description（通常是网站自己给的摘要，很干净）
        m = soup.find("meta", attrs={"name": "description"})
        if m and m.get("content"):
            t = " ".join(m["content"].split())
            if len(t) >= 20:
                return t

        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            t = " ".join(og["content"].split())
            if len(t) >= 20:
                return t

        # 兜底：第一个 <p>
        p = soup.find("p")
        if p:
            return " ".join(p.get_text(" ", strip=True).split())
    except Exception:
        return ""

    return ""


# ====== 4) 时间解析：保证排序稳定 ======
def parse_published(entry) -> tuple[str, float]:
    """
    返回 (published_string, published_ts)
    - published_ts 用于排序（数字）
    """
    # feedparser 通常给 published_parsed / updated_parsed
    for key in ("published_parsed", "updated_parsed"):
        tp = getattr(entry, key, None)
        if tp:
            try:
                dt = datetime.fromtimestamp(time.mktime(tp), tz=timezone.utc).astimezone()
                return dt.strftime("%Y-%m-%d %H:%M:%S%z"), dt.timestamp()
            except Exception:
                pass

    # 兜底：字符串解析
    for key in ("published", "updated"):
        txt = getattr(entry, key, "") or ""
        if txt:
            try:
                dt = date_parser.parse(txt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                dt = dt.astimezone()
                return dt.strftime("%Y-%m-%d %H:%M:%S%z"), dt.timestamp()
            except Exception:
                pass

    # 再兜底：当前时间
    dt = datetime.now().astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S%z"), dt.timestamp()


# ====== 5) 离线翻译（可选：仅英文 en->zh 常见可用） ======
def try_load_argos():
    try:
        import argostranslate.translate as at
        return at
    except Exception:
        return None


def detect_lang_by_url(url: str) -> str:
    """
    简单猜语言（只用于决定“是否尝试翻译”）
    - bbc -> en
    - nhk -> ja
    """
    host = urlparse(url).netloc.lower()
    if "bbc." in host:
        return "en"
    if "nhk.or.jp" in host:
        return "ja"
    return "unknown"


def translate_text(at, text: str, from_lang: str, to_lang: str) -> str:
    """
    使用 Argos 离线翻译。失败则返回空字符串。
    """
    if not at or not text.strip():
        return ""
    try:
        return at.translate(text, from_lang, to_lang)
    except Exception:
        return ""


# ====== 6) 主流程 ======
def main():
    at = try_load_argos()  # 可能为 None（没装就跳过翻译）
    items = []
    dedupe = set()

    for source, feed_url in RSS:
        feed = feedparser.parse(feed_url)
        entries = getattr(feed, "entries", [])[:MAX_PER_FEED]

        for e in entries:
            title = getattr(e, "title", "") or ""
            link = getattr(e, "link", "") or ""
            if not link:
                continue

            # 去重：按 link 去重
            if link in dedupe:
                continue
            dedupe.add(link)

            published, published_ts = parse_published(e)

            summary = first_paragraph(link)
            time.sleep(ARTICLE_SLEEP)

            # 中文字段：默认占位
            title_zh = "（中文翻译占位：暂无/未翻译）"
            summary_zh = "（中文摘要占位：暂无/未翻译）"

            # 如果能判断是英文，并且 argos 可用，则尝试 en->zh
            lang = detect_lang_by_url(link)
            if lang == "en" and at is not None:
                tzh = translate_text(at, title, "en", "zh")
                szh = translate_text(at, summary, "en", "zh")
                if tzh.strip():
                    title_zh = tzh
                if szh.strip():
                    summary_zh = szh

            # 日文目前保持占位（你要的是“占位”）
            items.append({
                "source": source,
                "published": published,
                "published_ts": published_ts,
                "title": title,
                "title_zh": title_zh,
                "link": link,
                "summary": summary,
                "summary_zh": summary_zh,
            })

    # 默认按最新排序（网页也能切换，但这里先排好）
    items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)

    data = {
        "updated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "items": items
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

