"""
Tender Monitor
Fetches TED (Tenders Electronic Daily) and SAM.gov opportunities,
summarises with Claude AI, and sends a weekly email.
"""

import os
import re
import json
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from xml.etree import ElementTree as ET

import requests
import anthropic

# ── Configuration ──────────────────────────────────────────────────────────────

RECIPIENT_EMAILS = ["vidir@istak.is", "hjalmur@istak.is", "karl@istak.is"]

SENDER_EMAIL   = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASSWORD"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TenderMonitorBot/1.0)"}

# ── TED fetcher ────────────────────────────────────────────────────────────────

TED_RSS_URL = (
    "https://ted.europa.eu/TED/rss/rssFeed.do"
    "?action=rss&cpv=45&countryCode=IS"
)
TED_API_URL = "https://ted.europa.eu/api/v3.0/notices/search"


def fetch_ted():
    """Try legacy RSS first; fall back to TED API v3 if the URL returns HTML."""
    try:
        r = requests.get(TED_RSS_URL, headers=HEADERS, timeout=30)
        ct = r.headers.get("Content-Type", "")
        body = r.text.lstrip()
        if "xml" in ct or body.startswith("<?xml") or body.startswith("<rss"):
            return _parse_ted_rss(r.content)
        print("  TED RSS URL returned HTML (old endpoint dead), trying TED API v3")
    except Exception as e:
        print(f"  TED RSS error: {e}, trying TED API v3")

    return _fetch_ted_api()


def _parse_ted_rss(content):
    items = []
    try:
        root = ET.fromstring(content)
        for el in root.iter("item"):
            items.append({
                "title":       (el.findtext("title") or "").strip(),
                "url":         (el.findtext("link") or "").strip(),
                "description": (el.findtext("description") or "").strip(),
                "published":   (el.findtext("pubDate") or "").strip(),
                "source":      "TED",
            })
    except Exception as e:
        print(f"  TED RSS parse error: {e}")
    print(f"  TED RSS: {len(items)} items")
    return items


def _fetch_ted_api():
    try:
        payload = {
            "query": "cpv:45 AND NC:IS",
            "fields": ["title", "url", "publicationDate", "description", "noticeNumber"],
            "size": 20,
            "sortField": "publicationDate",
            "sortOrder": "DESC",
        }
        r = requests.post(
            TED_API_URL,
            json=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        notices = (
            data.get("notices")
            or data.get("results")
            or data.get("hits", {}).get("hits", [])
            or []
        )
        items = []
        for n in notices:
            src = n.get("_source", n)
            title = src.get("title", "") or ""
            if isinstance(title, dict):
                title = title.get("en") or next(iter(title.values()), "")
            desc = src.get("description", "") or ""
            if isinstance(desc, dict):
                desc = desc.get("en") or next(iter(desc.values()), "")
            notice_no = src.get("noticeNumber", "")
            url = src.get("url", "") or (
                f"https://ted.europa.eu/udl?uri=TED:NOTICE:{notice_no}:TEXT:EN:HTML"
                if notice_no else ""
            )
            items.append({
                "title":       str(title).strip(),
                "url":         str(url).strip(),
                "description": str(desc).strip(),
                "published":   str(src.get("publicationDate", "")).strip(),
                "source":      "TED",
            })
        print(f"  TED API: {len(items)} items")
        return items
    except Exception as e:
        print(f"  TED API error: {e}")
        return []


# ── SAM.gov fetcher ────────────────────────────────────────────────────────────

SAM_URL = (
    "https://sam.gov/api/prod/sgs/v1/search/"
    "?index=opp&q=iceland&sort=-modifiedDate&mode=search&start=0&limit=10"
)


def fetch_samgov():
    """Fetch US government opportunities from SAM.gov related to Iceland."""
    try:
        r = requests.get(
            SAM_URL,
            headers={**HEADERS, "Accept-Encoding": "identity"},
            timeout=30,
        )
        r.raise_for_status()

        try:
            data = r.json()
        except Exception:
            print(f"  SAM.gov response not JSON (status {r.status_code}), skipping")
            return []

        # SAM.gov can return results under several different keys
        hits = (
            data.get("_embedded", {}).get("results", [])
            or data.get("results", [])
            or data.get("hits", {}).get("hits", [])
            or []
        )
        items = []
        for h in hits:
            src = h.get("_source", h)
            title  = src.get("title", "") or src.get("name", "") or ""
            desc   = src.get("description", "") or src.get("synopsis", "") or ""
            posted = src.get("postedDate", "") or src.get("modifiedDate", "") or ""
            link   = src.get("uiLink", "") or src.get("link", "") or ""
            if link and not link.startswith("http"):
                link = "https://sam.gov" + link
            items.append({
                "title":       str(title).strip(),
                "url":         str(link).strip(),
                "description": str(desc).strip(),
                "published":   str(posted).strip(),
                "source":      "SAM.gov",
            })
        print(f"  SAM.gov: {len(items)} items")
        return items
    except Exception as e:
        print(f"  SAM.gov error: {e}")
        return []


# ── Claude summariser ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an analyst for an Icelandic construction company (Ístak hf.) reviewing
tender notices and government procurement opportunities.

Assess whether each notice is relevant to a company doing civil engineering, infrastructure,
building construction, or related work in Iceland or for US military/government contracts
in Iceland or the Nordic/Arctic region (e.g. Keflavík/NAS Keflavik, USAF Iceland).

Respond in this exact JSON format with no markdown fences, just raw JSON:
{
  "is_relevant": true,
  "title_en": "Short English title",
  "summary_en": "2-3 sentence summary: type of work, estimated value if known, deadline",
  "relevance": "One sentence on why this matters to an Icelandic construction company"
}

If not relevant, respond with exactly: {"is_relevant": false}"""


def summarise_tender(item):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = (
        f"Source: {item['source']}\n"
        f"Title: {item['title']}\n"
        f"URL: {item['url']}\n"
        f"Published: {item['published']}\n"
        f"Description:\n{item['description'][:3000]}"
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        return json.loads(raw.strip())
    except Exception as e:
        print(f"  Claude error for '{item['title'][:60]}': {e}")
        return {"is_relevant": False}


# ── Email builder ──────────────────────────────────────────────────────────────

def build_email(ted_results, sam_results):
    week_str = datetime.now().strftime("%d %b %Y")

    rel_ted = [(i, s) for i, s in ted_results if s.get("is_relevant")]
    rel_sam = [(i, s) for i, s in sam_results if s.get("is_relevant")]
    total = len(rel_ted) + len(rel_sam)

    if total == 0:
        subject = f"[Tender Monitor] No relevant tenders — week of {week_str}"
        html = (
            f"<h2>Tender Monitor — {week_str}</h2>"
            f"<p>No new relevant tenders found this week on TED or SAM.gov.</p>"
        )
        return subject, html

    subject = (
        f"[Tender Monitor] {total} tender{'s' if total != 1 else ''} found"
        f" — week of {week_str}"
    )

    parts = [
        f"<h2>📋 Tender Monitor — {week_str}</h2>",
        f"<p><strong>{total} relevant tender{'s' if total != 1 else ''}</strong>"
        f" found this week.</p><hr>",
    ]

    if rel_ted:
        parts.append("<h3>🇪🇺 TED — Tenders Electronic Daily</h3>")
        for item, s in rel_ted:
            title = s.get("title_en") or item["title"]
            url   = item.get("url", "")
            title_html = (
                f"<a href='{url}' style='color:#003399;text-decoration:none'>{title}</a>"
                if url else title
            )
            parts.append(
                f"<div style='border-left:4px solid #003399;padding:10px 16px;"
                f"margin:12px 0;background:#f5f8ff'>"
                f"<strong>{title_html}</strong>"
                f"<p style='margin:6px 0'>{s.get('summary_en', '')}</p>"
                f"<p style='margin:4px 0;font-size:0.85em;color:#555'>"
                f"{s.get('relevance', '')}</p>"
                f"<p style='margin:4px 0;font-size:0.8em;color:#999'>"
                f"Published: {item.get('published') or 'N/A'}"
                + (f" &nbsp;|&nbsp; <a href='{url}' style='color:#003399'>View on TED →</a>" if url else "")
                + f"</p></div>"
            )
        parts.append("<br>")

    if rel_sam:
        parts.append("<h3>🇺🇸 SAM.gov — US Government Opportunities</h3>")
        for item, s in rel_sam:
            title = s.get("title_en") or item["title"]
            url   = item.get("url", "")
            title_html = (
                f"<a href='{url}' style='color:#B31942;text-decoration:none'>{title}</a>"
                if url else title
            )
            parts.append(
                f"<div style='border-left:4px solid #B31942;padding:10px 16px;"
                f"margin:12px 0;background:#fff5f6'>"
                f"<strong>{title_html}</strong>"
                f"<p style='margin:6px 0'>{s.get('summary_en', '')}</p>"
                f"<p style='margin:4px 0;font-size:0.85em;color:#555'>"
                f"{s.get('relevance', '')}</p>"
                f"<p style='margin:4px 0;font-size:0.8em;color:#999'>"
                f"Posted: {item.get('published') or 'N/A'}"
                + (f" &nbsp;|&nbsp; <a href='{url}' style='color:#B31942'>View on SAM.gov →</a>" if url else "")
                + f"</p></div>"
            )
        parts.append("<br>")

    parts.append(
        f"<hr><p style='color:#999;font-size:0.8em'>"
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} "
        f"| Sources: <a href='https://ted.europa.eu'>TED</a>"
        f" · <a href='https://sam.gov'>SAM.gov</a></p>"
    )

    return subject, "\n".join(parts)


# ── Email sender ───────────────────────────────────────────────────────────────

def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENT_EMAILS)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, GMAIL_APP_PASS)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAILS, msg.as_string())
    print(f"Email sent to {', '.join(RECIPIENT_EMAILS)}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Tender Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    print("── TED (Tenders Electronic Daily) ──────────────────────────")
    ted_raw = fetch_ted()
    ted_results = []
    for item in ted_raw:
        print(f"  Analysing: {item['title'][:70]}")
        s = summarise_tender(item)
        ted_results.append((item, s))
        if s.get("is_relevant"):
            print(f"  → RELEVANT: {s.get('title_en', '')[:60]}")

    print("\n── SAM.gov ──────────────────────────────────────────────────")
    sam_raw = fetch_samgov()
    sam_results = []
    for item in sam_raw:
        print(f"  Analysing: {item['title'][:70]}")
        s = summarise_tender(item)
        sam_results.append((item, s))
        if s.get("is_relevant"):
            print(f"  → RELEVANT: {s.get('title_en', '')[:60]}")

    subject, html = build_email(ted_results, sam_results)
    print(f"\n{'='*60}")
    print(f"Subject: {subject}")
    print(f"{'='*60}")
    send_email(subject, html)


if __name__ == "__main__":
    main()
