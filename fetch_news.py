# -*- coding: utf-8 -*-
"""
fetch_news.py
========================================
RSS 新闻抓取 + 第一段摘要 + 中文翻译（离线 Argos）

- RSS：
  - BBC World: http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml
  - NHK News(cat0): https://www3.nhk.or.jp/rss/news/cat0.xml

输出：
- 终端打印（可 --limit 控制）
- 写入 output/news_YYYYMMDD.json
- 可选写入站点数据 docs/news.json（用于 GitHub Pages）

翻译：
- 英文：en->zh
- 日文：ja->en->zh（链式翻译，解决 NHK 正文不翻译的问题）

命令：
- 安装离线翻译模型（需要联网下载模型包）：
    python fetch_news.py --install-models
- 只输出新增（默认）：
    python fetch_news.py --new --limit 3
- 输出全部：
    python fetch_news.py --all --limit 3
- 生成网站用数据：
    python fetch_news.py --all --site
"""

import argparse
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
# 1) 可配置项（新手建议只改这里）
# =========================

RSS_FEEDS = [
    {
        "name": "BBC World",
        "url": "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml"
    },
    {
        "name": "NHK News (cat0)",
        "url": "https://www3.nhk.or.jp/rss/news/cat0.xml"
    },
]

OUTPUT_DIR = "output"
DEFAULT_PRINT_LIMIT = 20

REQUEST_TIMEOUT_SECONDS = 12
REQUEST_RETRY_TIMES = 2
REQUEST_RETRY_SLEEP_SECONDS = 1

# 抓文章第一段的“节流”，避免被封（建议 0.2~1.0）
ARTICLE_FETCH_SLEEP_SECONDS = 0.35

# 增量记录
SEEN_FILE = "seen.json"

# 翻译缓存（避免重复翻）
TRANSLATE_CACHE_FILE = "translation_cache.json"


# =========================
# 2) 通用工具函数
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
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print_cn(f"⚠️ 写入失败：{path}（原因：{e}）")


def load_seen(file_path: str) -> Set[str]:
    data = load_json(file_path, {"seen": []})
    seen_list = data.get("seen", [])
    if not isinstance(seen_list, list):
        return set()
    return set(str(x) for x in seen_list)


def save_seen(file_path: str, seen_set: Set[str]) -> None:
    save_json(file_path, {"seen": sorted(seen_set)})


def parse_datetime_from_entry(entry: dict) -> Optional[datetime]:
    # feedparser 的 parsed 字段
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                ts = time.mktime(parsed)
                return datetime.fromtimestamp(ts).astimezone()
            except Exception:
                pass

    # 文本字段
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


def requests_get_with_retry(url: str, timeout: int, retry_times: int, retry_sleep: int) -> Optional[requests.Response]:
    headers = {
        "User-Agent": "michael-news-bot/1.0 (+rss fetcher)"
    }

    attempt_total = retry_times + 1
    for attempt in range(1, attempt_total + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if attempt < attempt_total:
                print_cn(f"⚠️ 抓取失败（第 {attempt}/{attempt_total} 次）：{e}")
                print_cn(f"   {retry_sleep} 秒后重试...")
                time.sleep(retry_sleep)
            else:
                print_cn(f"❌ 抓取失败（已重试 {retry_times} 次仍失败）：{e}")
                return None
    return None


def build_item_key(title: str, link: str) -> str:
    return link if link else title


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def make_output_filename(output_dir: str, fmt: str = "json") -> str:
    ensure_dir(output_dir)
    date_str = datetime.now().strftime("%Y%m%d")
    base = f"news_{date_str}.{fmt}"
    path = os.path.join(output_dir, base)
    if not os.path.exists(path):
        return path
    time_str = datetime.now().strftime("%H%M%S")
    base2 = f"news_{date_str}_{time_str}.{fmt}"
    return os.path.join(output_dir, base2)


# =========================
# 3) 网页正文第一段提取
# =========================

def extract_first_paragraph(url: str, html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # 去掉脚本/样式
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    host = urlparse(url).netloc.lower()

    def pick_first_p(container) -> str:
        if not container:
            return ""
        ps = container.find_all("p")
        for p in ps:
            t = normalize_text(p.get_text(" ", strip=True))
            # 过滤太短/像导航的
            if len(t) >= 25 and "cookie" not in t.lower():
                return t
        return ""

    # --- NHK ---
    if "nhk.or.jp" in host:
        candidates = [
            soup.select_one("#js-article-body"),
            soup.select_one(".content--detail-body"),
            soup.select_one("article"),
            soup.select_one("main"),
        ]
        for c in candidates:
            t = pick_first_p(c)
            if t:
                return t

    # --- BBC ---
    if "bbc." in host:
        c = soup.select_one("article") or soup.select_one("main")
        t = pick_first_p(c)
        if t:
            return t

    # 通用兜底
    c = soup.select_one("article") or soup.select_one("main") or soup
    t = pick_first_p(c)
    if t:
        return t

    # 再兜底：全站第一段
    for p in soup.find_all("p"):
        t = normalize_text(p.get_text(" ", strip=True))
        if len(t) >= 25:
            return t

    return ""


def fetch_first_paragraph(url: str) -> str:
    if not url:
        return ""
    resp = requests_get_with_retry(
        url=url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        retry_times=REQUEST_RETRY_TIMES,
        retry_sleep=REQUEST_RETRY_SLEEP_SECONDS
    )
    if resp is None:
        return ""
    html = resp.text
    return extract_first_paragraph(url, html)


# =========================
# 4) 离线翻译（Argos）
# =========================

ARGOS_AVAILABLE = False
try:
    import argostranslate.package
    import argostranslate.translate
    ARGOS_AVAILABLE = True
except Exception:
    ARGOS_AVAILABLE = False


def load_translate_cache() -> Dict[str, str]:
    data = load_json(TRANSLATE_CACHE_FILE, {})
    if not isinstance(data, dict):
        return {}
    # 限制一下大小，避免无限增长（可自行调）
    if len(data) > 30000:
        # 简单裁剪：只保留最后一部分（非严格 LRU，但够用）
        items = list(data.items())[-20000:]
        return dict(items)
    return data


def save_translate_cache(cache: Dict[str, str]) -> None:
    save_json(TRANSLATE_CACHE_FILE, cache)


def detect_lang_simple(text: str) -> str:
    """够用的粗检测：有假名 => ja；英文占比高 => en；否则默认 ja（NHK 多为汉字+假名）"""
    if not text:
        return "unknown"
    for ch in text:
        code = ord(ch)
        if (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF):
            return "ja"
    ascii_letters = sum(('a' <= c.lower() <= 'z') for c in text)
    if ascii_letters >= max(10, int(len(text) * 0.2)):
        return "en"
    return "ja"


def argos_has_pair(from_code: str, to_code: str) -> bool:
    if not ARGOS_AVAILABLE:
        return False
    langs = argostranslate.translate.get_installed_languages()
    src = next((l for l in langs if l.code == from_code), None)
    if not src:
        return False
    return any(t.code == to_code for t in src.translations)


def argos_translate(text: str, from_code: str, to_code: str) -> str:
    if not ARGOS_AVAILABLE or not text.strip():
        return ""
    try:
        langs = argostranslate.translate.get_installed_languages()
        src = next((l for l in langs if l.code == from_code), None)
        if not src:
            return ""
        tr = next((t for t in src.translations if t.code == to_code), None)
        if not tr:
            return ""
        return tr.translate(text)
    except Exception:
        return ""


def translate_to_zh(text: str, cache: Dict[str, str]) -> str:
    """
    免费离线翻译：
    - en -> zh
    - ja -> zh：优先 direct（如果未来有），否则 ja->en->zh
    """
    if not text or not text.strip():
        return ""

    lang = detect_lang_simple(text)
    key = f"{lang}::zh::{text}"
    if key in cache:
        return cache[key]

    # 英文直翻
    if lang == "en":
        out = argos_translate(text, "en", "zh")
        out = (out or "").strip()
        cache[key] = out
        return out

    # 日文：direct 或链式
    if lang == "ja":
        if argos_has_pair("ja", "zh"):
            out = argos_translate(text, "ja", "zh")
            out = (out or "").strip()
            cache[key] = out
            return out

        # 链式：ja -> en -> zh
        mid_key = f"ja::en::{text}"
        if mid_key in cache:
            mid = cache[mid_key]
        else:
            mid = argos_translate(text, "ja", "en")
            mid = (mid or "").strip()
            cache[mid_key] = mid

        if not mid:
            cache[key] = ""
            return ""

        out_key2 = f"en::zh::{mid}"
        if out_key2 in cache:
            out = cache[out_key2]
        else:
            out = argos_translate(mid, "en", "zh")
            out = (out or "").strip()
            cache[out_key2] = out

        cache[key] = out
        return out

    # 兜底
    out = argos_translate(text, "en", "zh")
    out = (out or "").strip()
    cache[key] = out
    return out


def install_argos_models() -> int:
    """
    安装 Argos 离线模型（需要联网下载）
    我们需要：
      - en -> zh（BBC）
      - ja -> en（NHK 链式翻译第一段/标题）
    """
    if not ARGOS_AVAILABLE:
        print_cn("❌ 你还没安装 argostranslate 或导入失败。")
        print_cn("   解决：python -m pip install argostranslate")
        return 1

    print_cn("🌏 正在更新 Argos 模型索引（需要联网下载模型）...")
    try:
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
    except Exception as e:
        print_cn(f"❌ 更新/读取模型索引失败：{e}")
        return 1

    need_pairs = [("en", "zh"), ("ja", "en")]

    installed_any = False
    for src, dst in need_pairs:
        if argos_has_pair(src, dst):
            print_cn(f"✅ 已存在模型：{src}->{dst}")
            continue

        pkg = next((p for p in available if p.from_code == src and p.to_code == dst), None)
        if not pkg:
            print_cn(f"⚠️ 未在索引中找到：{src}->{dst}")
            continue

        try:
            print_cn(f"⬇️ 发现模型 {src}->{dst}，开始下载并安装...")
            download_path = pkg.download()
            argostranslate.package.install_from_path(download_path)
            print_cn(f"✅ 已安装：{src}->{dst}")
            installed_any = True
        except Exception as e:
            print_cn(f"❌ 安装失败：{src}->{dst}，原因：{e}")

    if not installed_any:
        print_cn("✅ 模型检查完成（没有新增安装也没关系）。")
    else:
        print_cn("✅ 模型安装完成。")

    return 0


# =========================
# 5) RSS 抓取、合并、去重、增量
# =========================

def fetch_and_parse_one_feed(feed_name: str, feed_url: str) -> List[Dict]:
    print_cn(f"📰 正在抓取 {feed_name}：{feed_url}")

    resp = requests_get_with_retry(
        url=feed_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        retry_times=REQUEST_RETRY_TIMES,
        retry_sleep=REQUEST_RETRY_SLEEP_SECONDS
    )
    if resp is None:
        print_cn(f"⚠️ 跳过 {feed_name}（抓取失败）")
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
            "title": title,
            "link": link,
            "published": published_str,
            "source": source_name,
            "_published_ts": dt.timestamp(),
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


def cleanup_internal_fields(items: List[Dict]) -> List[Dict]:
    cleaned: List[Dict] = []
    for it in items:
        cleaned.append({
            "title": it.get("title", ""),
            "title_zh": it.get("title_zh", ""),
            "snippet": it.get("snippet", ""),
            "snippet_zh": it.get("snippet_zh", ""),
            "link": it.get("link", ""),
            "published": it.get("published", ""),
            "source": it.get("source", ""),
        })
    return cleaned


def write_json(file_path: str, items: List[Dict]) -> None:
    ensure_dir(os.path.dirname(file_path) or ".")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def print_items(items: List[Dict], limit: int) -> None:
    if not items:
        print_cn("（本次没有需要输出的新闻）")
        return

    print_cn("")
    print_cn(f"📌 终端展示最新 {min(limit, len(items))} 条：")
    print_cn("------------------------------------------------------------")

    for idx, it in enumerate(items[:limit], start=1):
        print_cn(f"{idx}. [{it.get('published', '')}] ({it.get('source', '')})")
        print_cn(f"   标题：{it.get('title', '')}")
        tz = safe_get_str(it.get("title_zh"), "")
        print_cn(f"   标题（中文）：{tz if tz else '（未翻译/翻译失败）'}")
        print_cn(f"   链接：{it.get('link', '')}")

        sn = safe_get_str(it.get("snippet"), "")
        snz = safe_get_str(it.get("snippet_zh"), "")

        if sn:
            print_cn(f"   摘要（第一段）：{sn}")
            print_cn(f"   摘要（中文）：{snz if snz else '（未翻译/翻译失败）'}")
        else:
            print_cn("   摘要（第一段）：（未提取到，可能是网站结构变化/反爬/网络问题）")
            print_cn("   摘要（中文）：（未翻译/翻译失败）")

        print_cn("")

    print_cn("------------------------------------------------------------")


# =========================
# 6) 命令行入口
# =========================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 RSS 新闻（第一段摘要 + 中文翻译 + 站点输出）")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--new", action="store_true", help="只输出新增（默认行为）")
    group.add_argument("--all", action="store_true", help="输出全部（不做增量过滤）")

    parser.add_argument("--limit", type=int, default=DEFAULT_PRINT_LIMIT, help="终端打印条数（默认 20）")
    parser.add_argument("--site", action="store_true", help="生成站点用 docs/news.json（建议配合 --all）")
    parser.add_argument("--install-models", action="store_true", help="安装 Argos 离线翻译模型（需联网下载）")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.install_models:
        sys.exit(install_argos_models())

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
        print_cn("❌ 没有抓到任何条目。请检查网络/RSS 链接是否可访问。")
        return

    merged_unique = merge_sort_dedupe(all_items)
    print_cn(f"🔁 合并后去重：{len(merged_unique)} 条（来自 {len(RSS_FEEDS)} 个源）")

    selected_items, updated_seen = filter_new_items(
        items=merged_unique,
        seen_before=seen_before,
        mode_new=mode_new
    )

    if mode_new:
        print_cn(f"🆕 新增新闻：{len(selected_items)} 条（默认只输出新增）")
    else:
        print_cn(f"📦 输出全部：{len(selected_items)} 条（不做增量）")

    # 抓第一段摘要
    if selected_items:
        print_cn(f"🧾 正在为本次输出的 {len(selected_items)} 条新闻抓取“第一段摘要”...")
        for i, it in enumerate(selected_items, start=1):
            link = safe_get_str(it.get("link"), "")
            if not link:
                it["snippet"] = ""
                continue
            print_cn(f"   [{i}/{len(selected_items)}] 抓摘要：{link}")
            it["snippet"] = fetch_first_paragraph(link)
            time.sleep(ARTICLE_FETCH_SLEEP_SECONDS)

    # 翻译
    if ARGOS_AVAILABLE:
        cache = load_translate_cache()
        # 只有在确实安装了 en->zh 时才翻译（否则全是空）
        if argos_has_pair("en", "zh"):
            print_cn("🌏 正在把标题与摘要翻译成中文（离线 Argos）...")
            for i, it in enumerate(selected_items, start=1):
                title = safe_get_str(it.get("title"), "")
                snip = safe_get_str(it.get("snippet"), "")
                if title:
                    print_cn(f"   [{i}/{len(selected_items)}] 翻译标题：{title[:40]}...")
                    it["title_zh"] = translate_to_zh(title, cache)
                else:
                    it["title_zh"] = ""
                if snip:
                    it["snippet_zh"] = translate_to_zh(snip, cache)
                else:
                    it["snippet_zh"] = ""
            save_translate_cache(cache)
        else:
            print_cn("⚠️ 你还没安装 Argos 的 en->zh 模型，将跳过翻译。")
            print_cn("   运行：python fetch_news.py --install-models")
            for it in selected_items:
                it["title_zh"] = ""
                it["snippet_zh"] = ""
    else:
        print_cn("⚠️ 未检测到 argostranslate，将跳过翻译。")
        print_cn("   解决：python -m pip install argostranslate")
        for it in selected_items:
            it["title_zh"] = ""
            it["snippet_zh"] = ""

    output_items = cleanup_internal_fields(selected_items)

    # 保存 output 文件
    ensure_dir(OUTPUT_DIR)
    out_path = make_output_filename(OUTPUT_DIR, "json")
    write_json(out_path, output_items)
    print_cn(f"💾 已保存到：{out_path}")

    # 保存站点数据 docs/news.json（建议用于 GitHub Pages）
    if args.site:
        ensure_dir("docs")
        site_path = os.path.join("docs", "news.json")
        write_json(site_path, output_items)
        print_cn(f"🌐 已生成站点数据：{site_path}")

    save_seen(SEEN_FILE, updated_seen)

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
        print_cn("把上面的报错复制给我，我能继续帮你修。")
        sys.exit(1)



