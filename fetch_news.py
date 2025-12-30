# -*- coding: utf-8 -*-
"""
Michael News - RSS 新闻站点生成器（极简版）
========================================
功能：
1) 抓取 BBC World + NHK(cat0) RSS
2) 访问每条新闻 link，提取“第一段”
3) 离线翻译成中文（无需 API Key）：
   - 英文：en -> zh
   - 日文：ja -> en -> zh（用中转，避免找不到 ja->zh 模型）
4) 生成静态网页 docs/index.html（GitHub Pages 可直接展示）
5) 每条新闻仅展示：
   标题原文（中文翻译）
   第一段原文（中文翻译）

用法：
- 本地跑：
    python fetch_news.py --all
    python fetch_news.py --new
- 安装离线模型（在 GitHub Actions 里也会跑）：
    python fetch_news.py --install-models
"""

import argparse
import json
import os
import re
import sys
import time
import html
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import feedparser
import requests
from dateutil import parser as date_parser
from bs4 import BeautifulSoup

# -------------------------
# 配置（你只需要改这里）
# -------------------------
RSS_FEEDS = [
    {"name": "BBC News", "url": "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml"},
    {"name": "NHKニュース", "url": "https://www3.nhk.or.jp/rss/news/cat0.xml"},
]

OUTPUT_DIR = "output"
SITE_DIR = "docs"  # GitHub Pages 使用 /docs
SEEN_FILE = "seen.json"
TRANSLATION_CACHE_FILE = "translation_cache.json"

REQUEST_TIMEOUT_SECONDS = 12
REQUEST_RETRY_TIMES = 2
REQUEST_RETRY_SLEEP_SECONDS = 1
ARTICLE_FETCH_SLEEP_SECONDS = 0.25

# 站点显示条数（网页会显示全部；终端可用 --limit 控制）
DEFAULT_PRINT_LIMIT = 10

# Tokyo 时区显示（GitHub Actions 默认 UTC，这里强制转 JST）
JST = timezone(timedelta(hours=9))

# -------------------------
# 通用工具
# -------------------------
def print_cn(msg: str) -> None:
    print(msg)

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def safe_get_str(v, default: str = "") -> str:
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def requests_get_with_retry(url: str) -> Optional[requests.Response]:
    headers = {"User-Agent": "michael-news-bot/1.0"}
    attempts = REQUEST_RETRY_TIMES + 1
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if i < attempts:
                print_cn(f"⚠️ 抓取失败（第 {i}/{attempts} 次）：{e}")
                print_cn(f"   {REQUEST_RETRY_SLEEP_SECONDS} 秒后重试...")
                time.sleep(REQUEST_RETRY_SLEEP_SECONDS)
            else:
                print_cn(f"❌ 抓取失败（已重试 {REQUEST_RETRY_TIMES} 次仍失败）：{e}")
                return None
    return None

def parse_datetime_from_entry(entry: dict) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                ts = time.mktime(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST)
            except Exception:
                pass
    for key in ("published", "updated"):
        text = entry.get(key)
        if text:
            try:
                dt = date_parser.parse(str(text))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(JST)
            except Exception:
                pass
    return datetime.now(tz=JST)

def build_item_key(title: str, link: str) -> str:
    return link if link else title

def load_seen() -> Set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        seen_list = data.get("seen", [])
        return set(str(x) for x in seen_list) if isinstance(seen_list, list) else set()
    except Exception:
        return set()

def save_seen(seen_set: Set[str]) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen_set)}, f, ensure_ascii=False, indent=2)

def load_translation_cache() -> Dict[str, str]:
    if not os.path.exists(TRANSLATION_CACHE_FILE):
        return {}
    try:
        with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def save_translation_cache(cache: Dict[str, str]) -> None:
    with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# -------------------------
# 文章第一段提取（NHK/BBC 优先规则）
# -------------------------
def extract_first_paragraph(url: str, html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    host = urlparse(url).netloc.lower()

    def pick_first_p(container) -> str:
        if not container:
            return ""
        for p in container.find_all("p"):
            t = normalize_text(p.get_text(" ", strip=True))
            # 过滤太短、像导航的段落
            if len(t) >= 25:
                return t
        return ""

    # NHK
    if "nhk.or.jp" in host:
        candidates = [
            soup.select_one("#js-article-body"),
            soup.select_one("article"),
            soup.select_one("main"),
        ]
        for c in candidates:
            t = pick_first_p(c)
            if t:
                return t

    # BBC
    if "bbc." in host:
        candidates = [
            soup.select_one("article"),
            soup.select_one("main"),
        ]
        for c in candidates:
            t = pick_first_p(c)
            if t:
                return t

    # 通用兜底：全站第一个够长的 p
    for p in soup.find_all("p"):
        t = normalize_text(p.get_text(" ", strip=True))
        if len(t) >= 25:
            return t

    return ""

def fetch_first_paragraph(url: str) -> str:
    if not url:
        return ""
    resp = requests_get_with_retry(url)
    if not resp:
        return ""
    try:
        text = extract_first_paragraph(url, resp.text)
        return text
    except Exception:
        return ""

# -------------------------
# 离线翻译（Argos Translate）
# -------------------------
def try_import_argos():
    try:
        import argostranslate.package  # noqa
        import argostranslate.translate  # noqa
        return True
    except Exception:
        return False

def install_argos_models() -> None:
    """
    安装离线模型：
    - en->zh（BBC）
    - ja->en（NHK 走日->英->中）
    注意：需要联网下载模型（只在安装时需要）
    """
    ok = try_import_argos()
    if not ok:
        print_cn("❌ 离线翻译模块导入失败。请先：python -m pip install argostranslate")
        sys.exit(1)

    import argostranslate.package as pkg

    print_cn("🌏 正在更新 Argos 模型索引（需要联网下载模型）...")
    pkg.update_package_index()
    available = pkg.get_available_packages()

    need = [("en", "zh"), ("ja", "en")]
    for f, t in need:
        found = None
        for p in available:
            if p.from_code == f and p.to_code == t:
                found = p
                break
        if not found:
            print_cn(f"⚠️ 未在索引中找到：{f}->{t}")
            continue
        print_cn(f"⬇️ 发现模型 {f}->{t}，开始下载并安装...")
        path = found.download()
        pkg.install_from_path(path)
        print_cn(f"✅ 已安装：{f}->{t}")

    print_cn("✅ 模型安装流程结束。")

def translate_text_offline(text: str, from_code: str, to_code: str,
                           cache: Dict[str, str]) -> str:
    text = safe_get_str(text, "")
    if not text:
        return ""
    key = f"{from_code}->{to_code}:{text}"
    if key in cache:
        return cache[key]

    import argostranslate.translate as tr

    try:
        translated = tr.translate(text, from_code, to_code)
        translated = normalize_text(translated)
        cache[key] = translated
        return translated
    except Exception:
        return ""

def translate_to_zh(original: str, lang: str, cache: Dict[str, str]) -> str:
    """
    lang = 'en' or 'ja'
    - en: en->zh
    - ja: ja->en->zh
    """
    if not original:
        return ""
    if lang == "en":
        return translate_text_offline(original, "en", "zh", cache)
    if lang == "ja":
        mid = translate_text_offline(original, "ja", "en", cache)
        if not mid:
            return ""
        return translate_text_offline(mid, "en", "zh", cache)
    return ""

# -------------------------
# RSS 抓取 & 合并去重
# -------------------------
def fetch_and_parse_one_feed(feed_name: str, feed_url: str) -> List[Dict]:
    print_cn(f"📰 正在抓取 {feed_name}：{feed_url}")
    resp = requests_get_with_retry(feed_url)
    if not resp:
        print_cn(f"❌ 跳过 {feed_name}（抓取失败）")
        return []

    parsed = feedparser.parse(resp.content)
    entries = parsed.get("entries", [])
    print_cn(f"✅ {feed_name} 抓取成功，解析到 {len(entries)} 条条目")

    items: List[Dict] = []
    for entry in entries:
        title = safe_get_str(entry.get("title"), "(无标题)")
        link = safe_get_str(entry.get("link"), "")
        dt = parse_datetime_from_entry(entry)

        item_key = build_item_key(title, link)
        items.append({
            "title": title,
            "link": link,
            "published": dt.strftime("%Y-%m-%d %H:%M:%S%z"),
            "_published_ts": dt.timestamp(),
            "source": feed_name,
            "_key": item_key,
        })
    return items

def merge_sort_dedupe(items: List[Dict]) -> List[Dict]:
    items_sorted = sorted(items, key=lambda x: x.get("_published_ts", 0), reverse=True)
    seen: Set[str] = set()
    out: List[Dict] = []
    for it in items_sorted:
        k = safe_get_str(it.get("_key"), "")
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out

def filter_new_items(items: List[Dict], seen_before: Set[str], mode_new: bool) -> Tuple[List[Dict], Set[str]]:
    updated = set(seen_before)
    if not mode_new:
        for it in items:
            updated.add(it["_key"])
        return items, updated

    new_items: List[Dict] = []
    for it in items:
        k = it["_key"]
        if k not in seen_before:
            new_items.append(it)
        updated.add(k)
    return new_items, updated

# -------------------------
# 生成极简网页（Michael News）
# -------------------------
def build_site_html(items: List[Dict]) -> str:
    now = datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M:%S %z")

    def esc(s: str) -> str:
        return html.escape(s or "", quote=False)

    rows = []
    for it in items:
        title = esc(it.get("title", ""))
        title_zh = esc(it.get("title_zh", ""))
        para = esc(it.get("summary", ""))
        para_zh = esc(it.get("summary_zh", ""))
        link = esc(it.get("link", ""))
        source = esc(it.get("source", ""))
        published = esc(it.get("published", ""))

        title_line = f'{title}（{title_zh}）' if title_zh else f'{title}（未翻译）'
        para_line = f'{para}（{para_zh}）' if para_zh else f'{para}（未翻译）'

        rows.append(f"""
        <div class="card">
          <div class="meta">{source} · {published}</div>
          <div class="title">{title_line}</div>
          <div class="para">{para_line}</div>
          <div class="link"><a href="{link}" target="_blank" rel="noopener">打开原文</a></div>
        </div>
        """)

    body = "\n".join(rows) if rows else '<div class="empty">今天没有抓到新闻。</div>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Michael News</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;
         margin:0;background:#fafafa;color:#111;}}
    .wrap{{max-width:980px;margin:24px auto;padding:0 16px;}}
    h1{{margin:0 0 8px 0;font-size:28px;}}
    .sub{{color:#555;margin-bottom:18px;}}
    .card{{background:#fff;border:1px solid #e7e7e7;border-radius:12px;
          padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,.03);}}
    .meta{{color:#666;font-size:13px;margin-bottom:10px;}}
    .title{{font-size:18px;font-weight:700;line-height:1.35;margin-bottom:10px;}}
    .para{{font-size:15px;line-height:1.7;color:#222;}}
    .link{{margin-top:10px;font-size:14px;}}
    a{{color:#0b57d0;text-decoration:none;}}
    a:hover{{text-decoration:underline;}}
    .empty{{padding:24px;background:#fff;border:1px dashed #ccc;border-radius:12px;color:#666;}}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>📰 Michael News</h1>
    <div class="sub">最后更新：{esc(now)} ｜ 共 {len(items)} 条</div>
    {body}
  </div>
</body>
</html>"""

def write_site_files(items: List[Dict]) -> None:
    ensure_dir(SITE_DIR)
    # 同时输出一个 json（可选，方便你调试）
    with open(os.path.join(SITE_DIR, "news.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    html_text = build_site_html(items)
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_text)

# -------------------------
# 主流程
# -------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--new", action="store_true", help="只输出新增（默认）")
    g.add_argument("--all", action="store_true", help="输出全部（不做增量）")
    p.add_argument("--limit", type=int, default=DEFAULT_PRINT_LIMIT, help="终端打印条数")
    p.add_argument("--install-models", action="store_true", help="安装离线翻译模型")
    return p.parse_args()

def main() -> None:
    args = parse_args()

    if args.install_models:
        install_argos_models()
        return

    mode_new = not args.all
    seen_before = load_seen()

    all_items: List[Dict] = []
    for feed in RSS_FEEDS:
        all_items.extend(fetch_and_parse_one_feed(feed["name"], feed["url"]))

    if not all_items:
        print_cn("❌ 没抓到任何条目。")
        return

    merged = merge_sort_dedupe(all_items)
    print_cn(f"🔁 合并后去重：{len(merged)} 条（来自 {len(RSS_FEEDS)} 个源）")

    selected, updated_seen = filter_new_items(merged, seen_before, mode_new)
    if mode_new:
        print_cn(f"🆕 新增新闻：{len(selected)} 条（默认只输出新增）")
    else:
        print_cn(f"📦 输出全部：{len(selected)} 条（不做增量）")

    # 抓第一段
    if selected:
        print_cn(f"🧾 正在为本次输出的 {len(selected)} 条新闻抓取“第一段摘要”...")
        for i, it in enumerate(selected, start=1):
            link = it.get("link", "")
            if not link:
                it["summary"] = ""
            else:
                print_cn(f"   [{i}/{len(selected)}] 抓摘要：{link}")
                it["summary"] = fetch_first_paragraph(link)
            time.sleep(ARTICLE_FETCH_SLEEP_SECONDS)

    # 离线翻译（如果 argos 没装，就跳过）
    cache = load_translation_cache()
    if try_import_argos():
        print_cn("🌏 正在把标题与摘要离线翻译成中文（无 Key）...")
        for i, it in enumerate(selected, start=1):
            source = it.get("source", "")
            lang = "ja" if "NHK" in source else "en"
            t = it.get("title", "")
            s = it.get("summary", "")
            if t:
                it["title_zh"] = translate_to_zh(t, lang, cache)
            else:
                it["title_zh"] = ""
            if s:
                it["summary_zh"] = translate_to_zh(s, lang, cache)
            else:
                it["summary_zh"] = ""
            if i % 10 == 0:
                save_translation_cache(cache)
        save_translation_cache(cache)
    else:
        print_cn("⚠️ 未检测到 argostranslate，跳过翻译。（你可以运行：python fetch_news.py --install-models）")
        for it in selected:
            it["title_zh"] = ""
            it["summary_zh"] = ""

    # 保存 output（可选）
    ensure_dir(OUTPUT_DIR)
    out_path = os.path.join(OUTPUT_DIR, f"news_{datetime.now(tz=JST).strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(selected, f, ensure_ascii=False, indent=2)
    print_cn(f"💾 已保存到：{out_path}")

    # 生成站点
    write_site_files(selected)
    print_cn(f"✅ 已生成站点：{SITE_DIR}/index.html")

    save_seen(updated_seen)

    # 终端简略打印
    limit = max(1, int(args.limit))
    print_cn(f"\n📌 终端展示最新 {min(limit, len(selected))} 条：")
    print_cn("-" * 60)
    for idx, it in enumerate(selected[:limit], start=1):
        print_cn(f"{idx}. [{it.get('published','')}] ({it.get('source','')})")
        print_cn(f"   标题：{it.get('title','')}")
        print_cn(f"   标题（中文）：{it.get('title_zh','(未翻译)')}")
        print_cn(f"   摘要：{it.get('summary','')}")
        print_cn(f"   摘要（中文）：{it.get('summary_zh','(未翻译)')}")
        print_cn(f"   链接：{it.get('link','')}\n")
    print_cn("-" * 60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_cn("\n🛑 你手动中断了程序（Ctrl+C）")
        sys.exit(0)


