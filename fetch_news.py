# -*- coding: utf-8 -*-
"""
fetch_news.py
========================================
生成 Michael News 网站数据（docs/news.json + docs/site_meta.json）

功能：
- 抓取 BBC World + NHK cat0 RSS
- 对每条新闻打开网页，提取“第一段”作为摘要
- 翻译成中文：
  - 英文：en -> zh
  - 日文：ja -> en -> zh（因为很多环境下找不到 ja->zh 模型）
- 结果写入 docs/news.json（供 GitHub Pages 静态网页读取）

用法（GitHub Actions 推荐）：
- 安装翻译模型（可失败，不影响后续生成）：
    python fetch_news.py --install-models
- 生成网站数据（全量）：
    python fetch_news.py --all

本地调试：
- 只看新增：
    python fetch_news.py --new --limit 5
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# =========================
# 1) 可配置项
# =========================

RSS_FEEDS = [
    {
        "name": "BBC News",
        "url": "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml",
    },
    {
        "name": "NHKニュース",
        "url": "https://www3.nhk.or.jp/rss/news/cat0.xml",
    },
]

REQUEST_TIMEOUT_SECONDS = 12
REQUEST_RETRY_TIMES = 2
REQUEST_RETRY_SLEEP_SECONDS = 1

ARTICLE_FETCH_SLEEP_SECONDS = 0.25

DEFAULT_PRINT_LIMIT = 20

SEEN_FILE = "seen.json"
TRANSLATION_CACHE_FILE = "translation_cache.json"

DOCS_DIR = "docs"
NEWS_JSON_PATH = os.path.join(DOCS_DIR, "news.json")
SITE_META_PATH = os.path.join(DOCS_DIR, "site_meta.json")

# =========================
# 2) 小工具
# =========================

def print_cn(msg: str) -> None:
    print(msg)

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def safe_get_str(value, default: str = "") -> str:
    if value is None:
        return default
    try:
        s = str(value).strip()
        return s if s else default
    except Exception:
        return default

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def looks_japanese(text: str) -> bool:
    # 粗略判断：出现假名/常用日文字符就当日文
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", text or ""))

def parse_datetime_from_entry(entry: dict) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                ts = time.mktime(parsed)
                return datetime.fromtimestamp(ts).astimezone()
            except Exception:
                pass

    for key in ("published", "updated"):
        text = entry.get(key)
        if text:
            try:
                dt = date_parser.parse(str(text))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                return dt.astimezone()
            except Exception:
                pass

    return None

def requests_get_with_retry(url: str) -> Optional[requests.Response]:
    headers = {"User-Agent": "michael-news-bot/1.0"}
    attempt_total = REQUEST_RETRY_TIMES + 1

    for attempt in range(1, attempt_total + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if attempt < attempt_total:
                print_cn(f"⚠️ 抓取失败（第 {attempt}/{attempt_total} 次）：{e}")
                print_cn(f"   {REQUEST_RETRY_SLEEP_SECONDS} 秒后重试...")
                time.sleep(REQUEST_RETRY_SLEEP_SECONDS)
            else:
                print_cn(f"❌ 抓取失败（已重试 {REQUEST_RETRY_TIMES} 次仍失败）：{e}")
                return None
    return None

def build_item_key(title: str, link: str) -> str:
    return link if link else title

# =========================
# 3) 抓文章“第一段”
# =========================

def extract_first_paragraph(url: str, html: str) -> str:
    """
    从文章页 HTML 提取“第一段”正文。
    优先站点规则，其次通用规则。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 去掉无用内容
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    host = urlparse(url).netloc.lower()

    def first_good_paragraph(container) -> str:
        if not container:
            return ""
        ps = container.find_all("p")
        for p in ps:
            t = normalize_text(p.get_text(" ", strip=True))
            # 过滤太短/导航类
            if len(t) >= 30:
                return t
        # 兜底：取第一个非空
        for p in ps:
            t = normalize_text(p.get_text(" ", strip=True))
            if t:
                return t
        return ""

    # ---- NHK ----
    if "nhk.or.jp" in host:
        candidates = [
            soup.select_one("#js-article-body"),
            soup.select_one(".content--detail-body"),
            soup.select_one("article"),
            soup.select_one("main"),
        ]
        for c in candidates:
            t = first_good_paragraph(c)
            if t:
                return t

    # ---- BBC ----
    if "bbc." in host:
        container = soup.select_one("main") or soup.select_one("article")
        t = first_good_paragraph(container)
        if t:
            return t

    # ---- 通用 ----
    container = soup.select_one("article") or soup.select_one("main")
    t = first_good_paragraph(container)
    if t:
        return t

    # ---- 最后兜底：全站 p ----
    for p in soup.find_all("p"):
        t = normalize_text(p.get_text(" ", strip=True))
        if len(t) >= 30:
            return t
    for p in soup.find_all("p"):
        t = normalize_text(p.get_text(" ", strip=True))
        if t:
            return t
    return ""

def fetch_first_paragraph(url: str) -> str:
    if not url:
        return ""
    resp = requests_get_with_retry(url)
    if resp is None:
        return ""
    html = resp.text
    return extract_first_paragraph(url, html)

# =========================
# 4) 离线翻译（Argos）
# =========================

def _try_import_argos():
    try:
        import argostranslate.package  # type: ignore
        import argostranslate.translate  # type: ignore
        return True
    except Exception:
        return False

ARGOS_AVAILABLE = _try_import_argos()

def load_translation_cache() -> Dict[str, str]:
    data = load_json(TRANSLATION_CACHE_FILE, default={})
    return data if isinstance(data, dict) else {}

def save_translation_cache(cache: Dict[str, str]) -> None:
    save_json(TRANSLATION_CACHE_FILE, cache)

def argos_installed_languages() -> Set[Tuple[str, str]]:
    """
    返回已安装语言对 (from_code, to_code)
    """
    if not ARGOS_AVAILABLE:
        return set()
    import argostranslate.translate  # type: ignore
    langs = argostranslate.translate.get_installed_languages()
    pairs = set()
    for l in langs:
        for t in l.translations:
            pairs.add((l.code, t.to_lang.code))
    return pairs

def argos_translate(text: str, from_code: str, to_code: str) -> Optional[str]:
    if not ARGOS_AVAILABLE:
        return None
    import argostranslate.translate  # type: ignore

    installed = argos_installed_languages()
    if (from_code, to_code) not in installed:
        return None

    try:
        return argostranslate.translate.translate(text, from_code, to_code)
    except Exception:
        return None

def translate_to_zh(text: str, prefer_lang: str) -> str:
    """
    prefer_lang: 'en' or 'ja' (来源语言的偏好)
    翻译逻辑：
    - 如果来源是英文：en->zh
    - 如果来源是日文：
        1) 尝试 ja->zh（如果有）
        2) 否则 ja->en 再 en->zh（推荐路径）
    """
    text = safe_get_str(text, "")
    if not text:
        return ""

    if not ARGOS_AVAILABLE:
        return ""

    cache = translate_to_zh._cache  # type: ignore
    key = sha1_text(f"{prefer_lang}||{text}")
    if key in cache:
        return cache[key]

    result = ""

    if prefer_lang == "en":
        r = argos_translate(text, "en", "zh")
        result = r or ""
    else:
        # ja source
        direct = argos_translate(text, "ja", "zh")
        if direct:
            result = direct
        else:
            mid = argos_translate(text, "ja", "en")
            if mid:
                final = argos_translate(mid, "en", "zh")
                result = final or ""

    cache[key] = result
    return result

translate_to_zh._cache = load_translation_cache()  # type: ignore

def install_argos_models() -> int:
    """
    安装需要的 Argos 模型：
    - en -> zh
    - ja -> en
    - (可选) ja -> zh（多数时候索引里没有，不强求）

    返回：
    - 0：执行完成（即使缺 ja->zh 也算成功）
    - 1：更新索引/下载严重失败
    """
    if not ARGOS_AVAILABLE:
        print_cn("❌ 未安装 argostranslate，跳过模型安装。")
        print_cn("   解决：python -m pip install argostranslate")
        return 1

    import argostranslate.package  # type: ignore

    def retry(fn, times=3, sleep_s=2):
        last_err = None
        for i in range(times):
            try:
                return fn()
            except Exception as e:
                last_err = e
                print_cn(f"⚠️ 模型索引/下载失败（第 {i+1}/{times} 次）：{e}")
                time.sleep(sleep_s)
        raise last_err  # type: ignore

    print_cn("🌏 正在更新 Argos 模型索引（需要联网下载模型）...")

    try:
        retry(argostranslate.package.update_package_index, times=3, sleep_s=2)
        available_packages = argostranslate.package.get_available_packages()
    except Exception as e:
        print_cn(f"❌ 更新模型索引失败：{e}")
        return 1

    def find_pkg(frm: str, to: str):
        for p in available_packages:
            if p.from_code == frm and p.to_code == to:
                return p
        return None

    wanted = [("en", "zh"), ("ja", "en"), ("ja", "zh")]

    for frm, to in wanted:
        pkg = find_pkg(frm, to)
        if not pkg:
            print_cn(f"⚠️ 未在索引中找到：{frm}->{to}")
            continue
        try:
            print_cn(f"⬇️ 发现模型 {frm}->{to}，开始下载并安装...")
            download_path = pkg.download()
            argostranslate.package.install_from_path(download_path)
            print_cn(f"✅ 已安装：{frm}->{to}")
        except Exception as e:
            print_cn(f"⚠️ 安装失败 {frm}->{to}：{e}")

    print_cn("✅ 模型安装流程结束（即使缺 ja->zh 也没关系，日文会走 ja->en->zh）。")
    return 0

# =========================
# 5) RSS 抓取/合并/增量
# =========================

def load_seen(file_path: str) -> Set[str]:
    data = load_json(file_path, default={"seen": []})
    seen_list = data.get("seen", []) if isinstance(data, dict) else []
    if not isinstance(seen_list, list):
        return set()
    return set(str(x) for x in seen_list)

def save_seen(file_path: str, seen_set: Set[str]) -> None:
    save_json(file_path, {"seen": sorted(seen_set)})

def fetch_and_parse_one_feed(feed_name: str, feed_url: str) -> List[Dict]:
    print_cn(f"📰 正在抓取 {feed_name}：{feed_url}")

    resp = requests_get_with_retry(feed_url)
    if resp is None:
        print_cn(f"❌ 跳过 {feed_name}（抓取失败）")
        return []

    parsed = feedparser.parse(resp.content)

    feed_title = safe_get_str(parsed.get("feed", {}).get("title"), default=feed_name)
    source_name = feed_title if feed_title else feed_name

    entries = parsed.get("entries", [])
    print_cn(f"✅ {feed_name} 抓取成功，解析到 {len(entries)} 条条目")

    now_dt = datetime.now().astimezone()
    items: List[Dict] = []

    for entry in entries:
        title = safe_get_str(entry.get("title"), default="(无标题)")
        link = safe_get_str(entry.get("link"), default="")

        dt = parse_datetime_from_entry(entry) or now_dt
        published_str = dt.strftime("%Y-%m-%d %H:%M:%S%z")
        if len(published_str) >= 5:
            published_str = published_str[:-5] + published_str[-5:-2] + ":" + published_str[-2:]

        item_key = build_item_key(title=title, link=link)

        items.append({
            "source": source_name,
            "published": published_str,
            "_published_ts": dt.timestamp(),
            "title": title,
            "link": link,
            "_key": item_key,
        })

    return items

def merge_sort_dedupe(items: List[Dict]) -> List[Dict]:
    items_sorted = sorted(items, key=lambda x: x.get("_published_ts", 0), reverse=True)
    seen_in_run: Set[str] = set()
    unique_items: List[Dict] = []

    for it in items_sorted:
        key = safe_get_str(it.get("_key"), default="")
        if not key:
            key = f"__empty__{it.get('_published_ts', 0)}"
        if key in seen_in_run:
            continue
        seen_in_run.add(key)
        unique_items.append(it)

    return unique_items

def filter_new_items(items: List[Dict], seen_before: Set[str], mode_new: bool) -> Tuple[List[Dict], Set[str]]:
    updated_seen = set(seen_before)

    if not mode_new:
        for it in items:
            k = safe_get_str(it.get("_key"), default="")
            if k:
                updated_seen.add(k)
        return items, updated_seen

    new_items: List[Dict] = []
    for it in items:
        k = safe_get_str(it.get("_key"), default="")
        if not k:
            continue
        if k not in seen_before:
            new_items.append(it)
        updated_seen.add(k)

    return new_items, updated_seen

# =========================
# 6) 构建网站数据
# =========================

def build_output_items(selected_items: List[Dict]) -> List[Dict]:
    """
    生成最终写入 docs/news.json 的结构：
    - title_orig / title_zh
    - summary_orig / summary_zh（第一段）
    """
    out: List[Dict] = []

    need_translate = ARGOS_AVAILABLE and len(argos_installed_languages()) > 0

    if selected_items:
        print_cn(f"🧾 正在为本次输出的 {len(selected_items)} 条新闻抓取“第一段摘要”...")
    for i, it in enumerate(selected_items, start=1):
        link = safe_get_str(it.get("link"), "")
        title = safe_get_str(it.get("title"), "")
        source = safe_get_str(it.get("source"), "")
        published = safe_get_str(it.get("published"), "")

        summary = ""
        if link:
            print_cn(f"   [{i}/{len(selected_items)}] 抓摘要：{link}")
            summary = fetch_first_paragraph(link)
            time.sleep(ARTICLE_FETCH_SLEEP_SECONDS)

        # 语言判定（优先用来源，其次看文本）
        is_nhk = "nhk" in source.lower()
        prefer_lang = "ja" if (is_nhk or looks_japanese(title + " " + summary)) else "en"

        title_zh = ""
        summary_zh = ""
        if need_translate:
            title_zh = translate_to_zh(title, prefer_lang=prefer_lang)
            summary_zh = translate_to_zh(summary, prefer_lang=prefer_lang)

        out.append({
            "source": source,
            "published": published,
            "link": link,
            "title_orig": title,
            "title_zh": title_zh,
            "summary_orig": summary,
            "summary_zh": summary_zh,
        })

    # 保存翻译缓存（很重要：加速 + 减少重复翻译）
    save_translation_cache(translate_to_zh._cache)  # type: ignore
    return out

def write_site(news_items: List[Dict]) -> None:
    ensure_dir(DOCS_DIR)
    save_json(NEWS_JSON_PATH, news_items)

    meta = {
        "site_title": "Michael News",
        "last_updated": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "count": len(news_items),
    }
    save_json(SITE_META_PATH, meta)

def print_items(items: List[Dict], limit: int) -> None:
    if not items:
        print_cn("（本次没有需要输出的新闻）")
        return

    print_cn("")
    print_cn(f"📌 终端展示最新 {min(limit, len(items))} 条：")
    print_cn("------------------------------------------------------------")
    for idx, it in enumerate(items[:limit], start=1):
        print_cn(f"{idx}. [{it.get('published', '')}] ({it.get('source', '')})")
        t0 = safe_get_str(it.get("title_orig"), "")
        tz = safe_get_str(it.get("title_zh"), "")
        s0 = safe_get_str(it.get("summary_orig"), "")
        sz = safe_get_str(it.get("summary_zh"), "")

        if tz:
            print_cn(f"   标题：{t0}（{tz}）")
        else:
            print_cn(f"   标题：{t0}（未翻译）")

        print_cn(f"   链接：{it.get('link', '')}")

        if sz:
            print_cn(f"   摘要：{s0}（{sz}）")
        else:
            # 允许摘要为空
            if s0:
                print_cn(f"   摘要：{s0}（未翻译）")
            else:
                print_cn("   摘要：（未提取到第一段，可能是网站结构变化/反爬/网络问题）")
        print_cn("")
    print_cn("------------------------------------------------------------")

# =========================
# 7) CLI
# =========================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 RSS 新闻，生成 Michael News 站点数据（带中文翻译）")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--new", action="store_true", help="只输出新增（默认行为）")
    group.add_argument("--all", action="store_true", help="输出全部（不做增量过滤）")

    parser.add_argument("--limit", type=int, default=DEFAULT_PRINT_LIMIT, help="终端打印条数（默认 20）")
    parser.add_argument("--install-models", action="store_true", help="安装/更新 Argos 翻译模型（需要联网）")
    return parser.parse_args()

def main() -> None:
    args = parse_args()

    if args.install_models:
        code = install_argos_models()
        # 不强制失败：让 Actions 更稳定（即使网络抽风也不影响后续生成）
        sys.exit(0 if code == 0 else 0)

    mode_new = True
    if args.all:
        mode_new = False

    seen_before = load_seen(SEEN_FILE)

    all_items: List[Dict] = []
    for feed in RSS_FEEDS:
        name = safe_get_str(feed.get("name"), default="(未命名RSS)")
        url = safe_get_str(feed.get("url"), default="")
        items = fetch_and_parse_one_feed(feed_name=name, feed_url=url)
        all_items.extend(items)

    if not all_items:
        print_cn("⚠️ 没有抓到任何条目。请检查网络或 RSS 链接是否可用。")
        # 仍然写一个空站点，避免网页崩
        write_site([])
        return

    merged_unique = merge_sort_dedupe(all_items)
    print_cn(f"🔁 合并后去重：{len(merged_unique)} 条（来自 {len(RSS_FEEDS)} 个源）")

    selected_items, updated_seen = filter_new_items(
        items=merged_unique,
        seen_before=seen_before,
        mode_new=mode_new,
    )

    if mode_new:
        print_cn(f"🆕 新增新闻：{len(selected_items)} 条（默认只输出新增）")
    else:
        print_cn(f"📦 输出全部：{len(selected_items)} 条（不做增量）")

    # 构建最终输出
    output_items = build_output_items(selected_items if mode_new else merged_unique)

    # 写站点数据（网页读取 docs/news.json）
    write_site(output_items)

    # 更新 seen
    save_seen(SEEN_FILE, updated_seen)

    # 终端展示
    limit = max(1, int(args.limit))
    print_items(output_items, limit=limit)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_cn("\n🛑 你手动中断了程序（Ctrl+C）")
        sys.exit(0)
    except Exception as e:
        print_cn(f"\n❌ 程序发生未捕获异常：{e}")
        sys.exit(1)
