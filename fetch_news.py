# -*- coding: utf-8 -*-
import json, time
from datetime import datetime, timezone
import feedparser
import requests
from bs4 import BeautifulSoup

RSS = [
  ("BBC World", "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml"),
  ("NHK", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
]

OUT = "docs/news.json"

def first_paragraph(url: str) -> str:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent":"rss-news-site/1.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script","style","noscript"]):
            tag.decompose()

        # meta description 优先
        m = soup.find("meta", attrs={"name":"description"})
        if m and m.get("content"):
            t = " ".join(m["content"].split())
            if len(t) >= 20:
                return t

        og = soup.find("meta", attrs={"property":"og:description"})
        if og and og.get("content"):
            t = " ".join(og["content"].split())
            if len(t) >= 20:
                return t

        # 兜底：第一个 p
        p = soup.find("p")
        if p:
            return " ".join(p.get_text(" ", strip=True).split())
    except Exception:
        return ""
    return ""

def main():
    items = []
    for source, url in RSS:
        feed = feedparser.parse(url)
        for e in feed.entries[:30]:
            title = getattr(e, "title", "")
            link = getattr(e, "link", "")
            published = getattr(e, "published", "") or getattr(e, "updated", "")
            summary = first_paragraph(link) if link else ""
            items.append({
                "source": source,
                "published": published,
                "title": title,
                "link": link,
                "summary": summary
            })
            time.sleep(0.2)

    data = {
        "updated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "items": items
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
