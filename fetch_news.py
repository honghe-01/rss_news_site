#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RSS News -> Static site data builder (GitHub Pages)

目标：
- 抓取 RSS（BBC World + NHK cat0）
- 为每条新闻抓取“第一段原文”
- 标题 & 第一段都翻译成中文
  - BBC: en -> zh 直接
  - NHK: ja -> en -> zh（因为通常没有 ja->zh 模型）
- 生成 docs/data.json 给 GitHub Pages 站点使用

用法（本地/Actions）：
- 安装模型（Actions 用）：python fetch_news.py --install-models
- 构建站点数据：python fetch_news.py --build-site --limit 50
- 终端查看：python fetch_news.py --all --limit 3
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# -------------------------
# 配置：RSS 源
# -------------------------
SOURCES = [
    {
        "name": "BBC News",
        "lang": "en",
        "rss": "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml",
    },
    {
        "name": "NHKニュース",
        "lang": "ja",
        "rss": "https://www3.nhk.or.jp/rss/news/cat0.xml",
    },
]

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; MichaelNewsBot/1.0; +https://github.com/)"
}

DEFAULT_TIMEOUT = 15
RETRY = 3
SLEEP_BETWEEN = 1

DATA_OUT_PATH = os.path.join("docs", "data.json")


# -------------------------
# 工具：输出
# -------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + "..."


def parse_dt(entry: Any) -> Optional[datetime]:
    # feedparser 可能给 published / updated / created
    for k in ("published", "updated", "created"):
        if k in entry and entry[k]:
            try:
                dt = dateparser.parse(entry[k])
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    return None


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    # 输出 ISO + 时区
    return dt.astimezone().isoformat(timespec="seconds")


# -------------------------
# 翻译：Argos（离线）
# -------------------------
def _import_argos():
    try:
        import argostranslate.package  # noqa
        import argostranslate.translate  # noqa
        return True
    except Exception:
        return False


ARGOS_AVAILABLE = _import_argos()


def translate_argos(text: str, from_code: str, to_code: str) -> Optional[str]:
    """
    使用 Argos Translate 翻译。
    注意：Argos 没有时会返回 None；模型缺失也会异常 -> None
    """
    if not text:
        return ""
    if not ARGOS_AVAILABLE:
        return None
    try:
        import argostranslate.translate as atranslate

        return normalize_ws(atranslate.translate(text, from_code, to_code))
    except Exception:
        return None


def translate_to_zh(text: str, src_lang: str) -> Optional[str]:
    """
    统一翻译到中文（zh）
    - en -> zh：直接
    - ja -> zh：优先直接；若失败则 ja->en 再 en->zh
    """
    if not text:
        return ""
    text = normalize_ws(text)

    if src_lang == "en":
        return translate_argos(text, "en", "zh")

    if src_lang == "ja":
        direct = translate_argos(text, "ja", "zh")
        if direct:
            return direct
        # 中转：ja -> en -> zh
        mid = translate_argos(text, "ja", "en")
        if not mid:
            return None
        return translate_argos(mid, "en", "zh")

    # 其他语言：先不处理
    return None


def install_argos_models() -> None:
    """
    Actions 中安装模型：
    - en -> zh
    - ja -> en  （用于 NHK 中转）
    """
    if not ARGOS_AVAILABLE:
        log("❌ 未安装 argostranslate，无法安装模型。请先 pip install argostranslate")
        sys.exit(1)

    import argostranslate.package as ap

    log("🌏 正在更新 Argos 模型索引（需要联网下载模型）...")
    ap.update_package_index()
    pkgs = ap.get_available_packages()

    wanted = {("en", "zh"), ("ja", "en")}
    installed = []

    for f, t in wanted:
        pkg = next((p for p in pkgs if p.from_code == f and p.to_code == t), None)
        if not pkg:
            log(f"⚠️ 未在索引中找到：{f}->{t}")
            continue
        log(f"⬇️ 发现模型 {f}->{t}，开始下载并安装...")
        ap.install_from_path(pkg.download())
        installed.append(f"{f}->{t}")
        log(f"✅ 已安装：{f}->{t}")

    if installed:
        log("✅ 模型安装完成：" + ", ".join(installed))
    else:
        log("⚠️ 本次没有安装任何模型（可能索引缺失或网络问题）")


# -------------------------
# 抓取第一段摘要
# -------------------------
def http_get(url: str) -> Optional[str]:
    for i in range(RETRY):
        try:
            r = requests.get(url, headers=UA, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or r.encoding
            return r.text
        except Exception as e:
            if i < RETRY - 1:
                log(f"⚠️ 抓取失败（第 {i+1}/{RETRY} 次）：{e}")
                time.sleep(SLEEP_BETWEEN)
            else:
                log(f"❌ 抓取失败：{e}")
                return None
    return None


def extract_first_paragraph_bbc(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # BBC 新版常见结构：data-component="text-block" 里有 p
    candidates = []
    for p in soup.select('[data-component="text-block"] p'):
        t = normalize_ws(p.get_text(" ", strip=True))
        if len(t) >= 20:
            candidates.append(t)

    if not candidates:
        # fallback：全站第一个够长的 p
        for p in soup.find_all("p"):
            t = normalize_ws(p.get_text(" ", strip=True))
            if len(t) >= 20:
                candidates.append(t)

    return candidates[0] if candidates else ""


def extract_first_paragraph_nhk(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # NHK 常见正文容器：id/news_textbody 或 class 包含 body
    candidates = []

    body = soup.find(id="news_textbody")
    if body:
        for p in body.find_all("p"):
            t = normalize_ws(p.get_text(" ", strip=True))
            if len(t) >= 15:
                candidates.append(t)

    if not candidates:
        # fallback：找 main/article 下的 p
        for p in soup.select("article p, main p"):
            t = normalize_ws(p.get_text(" ", strip=True))
            if len(t) >= 15:
                candidates.append(t)

    return candidates[0] if candidates else ""


def fetch_first_paragraph(url: str, source_name: str) -> str:
    html = http_get(url)
    if not html:
        return ""
    if "bbc" in (url or "").lower() or source_name == "BBC News":
        return extract_first_paragraph_bbc(html)
    if "nhk" in (url or "").lower() or source_name == "NHKニュース":
        return extract_first_paragraph_nhk(html)
    # fallback
    soup = BeautifulSoup(html, "lxml")
    for p in soup.find_all("p"):
        t = normalize_ws(p.get_text(" ", strip=True))
        if len(t) >= 20:
            return t
    return ""


# -------------------------
# 数据结构
# -------------------------
@dataclass
class NewsItem:
    source: str
    source_lang: str
    title: str
    link: str
    published_at: str
    summary: str
    title_zh: str
    summary_zh: str


def item_to_dict(x: NewsItem) -> Dict[str, Any]:
    return {
        "source": x.source,
        "source_lang": x.source_lang,
        "title": x.title,
        "title_zh": x.title_zh,
        "link": x.link,
        "published_at": x.published_at,
        "summary": x.summary,
        "summary_zh": x.summary_zh,
    }


# -------------------------
# 主流程
# -------------------------
def fetch_all_entries() -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    返回 [(source_config, entry_dict), ...]
    """
    all_entries = []
    for src in SOURCES:
        log(f"📰 正在抓取 {src['name']}：{src['rss']}")
        feed = feedparser.parse(src["rss"])
        if feed.bozo:
            log(f"⚠️ RSS 解析警告：{getattr(feed, 'bozo_exception', '')}")
        entries = feed.entries or []
        log(f"✅ {src['name']} 抓取成功，解析到 {len(entries)} 条条目")
        for e in entries:
            all_entries.append((src, e))
    return all_entries


def dedup_entries(entries: List[Tuple[Dict[str, Any], Any]]) -> List[Tuple[Dict[str, Any], Any]]:
    seen = set()
    out = []
    for src, e in entries:
        link = (getattr(e, "link", None) or e.get("link") or "").strip()
        if not link:
            continue
        if link in seen:
            continue
        seen.add(link)
        out.append((src, e))
    return out


def sort_entries(entries: List[Tuple[Dict[str, Any], Any]]) -> List[Tuple[Dict[str, Any], Any]]:
    def key_fn(pair):
        src, e = pair
        dt = parse_dt(e)
        return dt.timestamp() if dt else 0.0

    return sorted(entries, key=key_fn, reverse=True)


def build_items(entries: List[Tuple[Dict[str, Any], Any]], limit: int) -> List[NewsItem]:
    entries = sort_entries(entries)[:limit]

    log(f"🧾 正在为本次输出的 {len(entries)} 条新闻抓取“第一段摘要”...")
    items: List[NewsItem] = []
    for i, (src, e) in enumerate(entries, 1):
        title = normalize_ws((getattr(e, "title", None) or e.get("title") or "").strip())
        link = (getattr(e, "link", None) or e.get("link") or "").strip()
        dt = parse_dt(e)
        published_at = fmt_dt(dt)

        log(f"   [{i}/{len(entries)}] 抓摘要：{link}")
        first_para = fetch_first_paragraph(link, src["name"])
        first_para = normalize_ws(first_para)

        # 翻译（标题 + 摘要）
        title_zh = translate_to_zh(title, src["lang"]) or "（未翻译/翻译失败）"
        summary_zh = translate_to_zh(first_para, src["lang"]) or "（未翻译/翻译失败）"

        items.append(
            NewsItem(
                source=src["name"],
                source_lang=src["lang"],
                title=title,
                link=link,
                published_at=published_at,
                summary=first_para,
                title_zh=title_zh,
                summary_zh=summary_zh,
            )
        )
    return items


def render_terminal(items: List[NewsItem], n: int) -> None:
    show = items[:n]
    log("")
    log(f"📌 终端展示最新 {len(show)} 条：")
    log("-" * 60)
    for idx, it in enumerate(show, 1):
        log(f"{idx}. [{it.published_at}] ({it.source})")
        log(f"   标题：{it.title}")
        log(f"   标题（中文）：{it.title_zh}")
        log(f"   链接：{it.link}")
        log(f"   摘要（第一段）：{it.summary}")
        log(f"   摘要（中文）：{it.summary_zh}")
        log("")
    log("-" * 60)


def write_site_data(items: List[NewsItem]) -> None:
    safe_mkdir("docs")

    now = datetime.now(timezone.utc).astimezone()
    payload = {
        "site_title": "Michael News",
        "generated_at": now.isoformat(timespec="seconds"),
        "count": len(items),
        "items": [item_to_dict(x) for x in items],
    }
    with open(DATA_OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"💾 已生成站点数据：{DATA_OUT_PATH}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-models", action="store_true", help="安装 Argos 翻译模型（Actions 用）")
    ap.add_argument("--all", action="store_true", help="输出全部（不做增量）")
    ap.add_argument("--new", action="store_true", help="（保留参数，但此精简版本不做增量）")
    ap.add_argument("--limit", type=int, default=50, help="最多处理多少条（默认 50）")
    ap.add_argument("--build-site", action="store_true", help="生成 docs/data.json（用于 GitHub Pages）")
    ap.add_argument("--print", action="store_true", help="终端打印最新 3 条（默认不开）")
    args = ap.parse_args()

    if args.install_models:
        install_argos_models()
        return

    entries = fetch_all_entries()
    entries = dedup_entries(entries)

    # 这个精简版默认不做增量，--new 只是兼容你原来的命令
    entries = sort_entries(entries)
    items = build_items(entries, limit=args.limit)

    if args.build_site:
        write_site_data(items)

    if args.print:
        render_terminal(items, n=min(3, len(items)))


if __name__ == "__main__":
    main()
