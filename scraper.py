"""
Municipal Meeting Monitor
Scrapes Reykjanesbær and Reykjavík fundargerðir for construction/tender mentions.
Sends a weekly digest email via Gmail SMTP.
"""

import os
import re
import smtplib
import json
import urllib3
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
import anthropic

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Configuration ─────────────────────────────────────────────────────────────

RECIPIENT_EMAILS = ["vidir@istak.is"]

SENDER_EMAIL   = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASSWORD"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]

LOOKBACK_DAYS = 8

MUNICIPALITIES = [
    {
        "name": "Reykjanesbær",
        "index_url": "https://www.reykjanesbaer.is/is/stjornsysla/stjornsyslan/fundargerdir",
        "base_url":  "https://www.reykjanesbaer.is",
        "type":      "reykjanesbaer",
        "priority_committees": [
            "umhverfis-og-skipulagsrad",
            "afgreidslufundur-byggingarfulltrua",
            "baejarrad",
            "baejarstjorn",
            "atvinnu-og-hafnarrad",
            "stjorneignasjodsrnb",
        ],
    },
    {
        "name": "Reykjavík",
        "index_url": "https://reykjavik.is/fundargerdir",
        "base_url":  "https://reykjavik.is",
        "type":      "reykjavik",
        "priority_committees": [
            "umhverfis-og-skipulagsrad",
            "afgreidslufundir-skipulagsfulltrua",
            "innkaupa-og-framkvaemdarad",
            "innkauparad",
            "borgarrad",
            "borgarstjorn",
            "skipulags-og-samgongurad",
        ],
    },
]

# ── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert analyst monitoring Icelandic municipal meeting minutes
for a construction company interested in COMMERCIAL opportunities only.

INCLUDE these types of items:
- Public tenders and procurement (utbod, innkaup) of any size
- Large infrastructure projects (roads, utilities, sewage, public spaces)
- New commercial or industrial zoning/planning changes (deiliskipulag, adalskipulag)
- Public building projects (schools, sports facilities, community buildings)
- Large residential developments (10+ units, apartment blocks)
- Harbour construction, port development and marine infrastructure (hofn, hafngardur,
  bryggja, Njarðvíkurhöfn, Helguvíkurhöfn) - always include regardless of size
- Land allocation for commercial/industrial use

EXCLUDE these types of items:
- Single family home permits (einbylishus)
- Small home extensions or renovations
- Advertising signs (auglysningaskilti)
- Small garage or shed permits
- Anything clearly for a private individual homeowner

If unsure whether something is relevant, ALWAYS include it. It is better to
include too much than to miss a real opportunity.

Respond in this exact JSON format with no markdown fences, no backticks, just raw JSON:
{
  "has_relevant_items": true,
  "items": [
    {
      "title_en": "Short English title",
      "summary_en": "2-3 sentence English summary of what was decided/discussed",
      "quote_is": "The most relevant original Icelandic sentence or two from the text",
      "type": "one of: tender | infrastructure | zoning | public_building | large_residential | land | other",
      "status": "one of: approved | rejected | under_review | advertised | for_info"
    }
  ]
}

If nothing relevant found, respond with exactly: {"has_relevant_items": false, "items": []}"""

# ── Helpers ───────────────────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MunicipalMonitorBot/1.0)"}


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  Could not fetch {url}: {e}")
        return None


def parse_icelandic_date(text):
    MONTHS = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
        "jul": 7, "agu": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12,
    }
    text = text.lower().strip()
    if "," in text:
        text = text.split(",", 1)[1].strip()
    text = re.sub(r'\.\s', ' ', text).strip()
    # normalise icelandic characters for month matching
    text = text.replace("í", "i").replace("ú", "u").replace("ý", "y")
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        day = int(parts[0].rstrip("."))
        month = next((v for k, v in MONTHS.items() if parts[1].startswith(k)), None)
        year = int(parts[2])
        if month:
            return datetime(year, month, day, tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def is_recent(dt, days=None):
    if days is None:
        days = LOOKBACK_DAYS
    if dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def is_priority(url, committee_slugs):
    return any(slug in url for slug in committee_slugs)


# ── Index scrapers ────────────────────────────────────────────────────────────

def get_meetings_reykjanesbaer(muni):
    soup = fetch(muni["index_url"])
    if not soup:
        return []
    meetings = []
    for a in soup.select("a[href*='/fundargerdir/']"):
        href = a["href"]
        if href.count("/") < 6:
            continue
        text = a.get_text(" ", strip=True)
        date_match = re.search(r'(\d{1,2}\.\s?\w+\.?\s+\d{4})', text)
        dt = parse_icelandic_date(date_match.group(1)) if date_match else None
        url = muni["base_url"] + href if href.startswith("/") else href
        meetings.append({"url": url, "title": text, "date": dt, "committee": href})
    return meetings


def get_meetings_reykjavik(muni):
    """Use Reykjavik's official open API."""
    try:
        r = requests.get(
            "https://api.reykjavik.is/gateway/meeting-documents/v1/api/meetings_list",
            headers=HEADERS,
            timeout=20,
            verify=False,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Reykjavik API error: {e}")
        return []

    print(f"  Reykjavik API returned {len(data)} items")
    if data:
        print(f"  Sample item keys: {list(data[0].keys())}")
        print(f"  Sample item: {data[0]}")

    meetings = []
    for item in data:
        # This API returns documents, use 'updated' as the date
        date_str = str(item.get("updated") or item.get("created") or "")
        dt = None
        try:
            dt = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass

        # Build URL from documentId
        doc_id = item.get("documentId", "")
        url = f"https://reykjavik.is/fundargerdir/{doc_id}" if doc_id else ""

        committee = item.get("groupName") or ""
        title = f"{committee} - fundur"

        meetings.append({
            "url": url,
            "title": title,
            "date": dt,
            "committee": committee.lower().replace(" ", "-").replace("\u2013", "-"),
        })
    return meetings


def get_meetings(muni):
    if muni["type"] == "reykjanesbaer":
        return get_meetings_reykjanesbaer(muni)
    return get_meetings_reykjavik(muni)


# ── Meeting content fetcher ───────────────────────────────────────────────────

def get_meeting_text(url):
    soup = fetch(url)
    if not soup:
        return ""
    main = soup.find("main") or soup.find(id="main") or soup.find("article")
    if main:
        for tag in main.select("nav, .sidebar, .breadcrumb, footer, script, style"):
            tag.decompose()
        return main.get_text("\n", strip=True)
    return soup.get_text("\n", strip=True)[:8000]


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyse_meeting(title, url, text):
    if not text.strip():
        print("  Empty text, skipping")
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"Meeting: {title}\nURL: {url}\n\n---\n\n{text[:12000]}"

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # strip markdown fences if present
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        raw = raw.strip()
        print(f"  Claude response preview: {raw[:120]}")
        data = json.loads(raw)
        if data.get("has_relevant_items"):
            return data.get("items", [])
        return []
    except Exception as e:
        print(f"  Claude error for {url}: {e}")
        return []


# ── Email builder ─────────────────────────────────────────────────────────────

STATUS_EMOJI = {
    "approved":     "✅",
    "rejected":     "❌",
    "under_review": "🔍",
    "advertised":   "📢",
    "for_info":     "ℹ️",
}
TYPE_LABEL = {
    "tender":            "📋 Tender / Procurement",
    "infrastructure":    "🔧 Infrastructure",
    "zoning":            "🗺 Zoning / Planning",
    "public_building":   "🏛 Public Building",
    "large_residential": "🏢 Large Residential",
    "land":              "📍 Land / Lots",
    "other":             "📌 Other",
}


def build_email(results):
    week_str = datetime.now().strftime("%d %b %Y")
    total = sum(len(r["items"]) for r in results if r["items"])

    if total == 0:
        subject = f"[Municipal Monitor] No relevant items — week of {week_str}"
        html = (
            f"<h2>Municipal Monitor — {week_str}</h2>"
            f"<p>No new commercial construction projects, tenders, or planning items found this week.</p>"
        )
        return subject, html

    subject = f"[Municipal Monitor] {total} item{'s' if total != 1 else ''} found — week of {week_str}"

    parts = [
        f"<h2>🏙 Municipal Monitor — {week_str}</h2>",
        f"<p><strong>{total} relevant item{'s' if total != 1 else ''}</strong> found this week.</p><hr>",
    ]

    for r in results:
        if not r["items"]:
            continue
        parts.append(
            f"<h3>{r['municipality']} — "
            f"<a href='{r['url']}'>{r['meeting_title']}</a></h3>"
        )
        parts.append(f"<p style='color:#666;font-size:0.9em'>{r['date_str']}</p>")
        for item in r["items"]:
            emoji = STATUS_EMOJI.get(item.get("status", ""), "•")
            type_label = TYPE_LABEL.get(item.get("type", "other"), "📌 Other")
            status_text = item.get("status", "").replace("_", " ").title()
            parts.append(
                f"<div style='border-left:4px solid #2562AE;padding:10px 16px;"
                f"margin:12px 0;background:#f9f9f9'>"
                f"<div style='font-size:0.85em;color:#555;margin-bottom:4px'>"
                f"{type_label} &nbsp;|&nbsp; {emoji} {status_text}</div>"
                f"<strong>{item.get('title_en', '')}</strong>"
                f"<p style='margin:4px 0'>"
                f"<a href='{r['url']}' style='font-size:0.85em;color:#2562AE'>"
                f"View meeting minutes →</a></p>"
                f"<p style='margin:6px 0'>{item.get('summary_en', '')}</p>"
                f"<blockquote style='border-left:3px solid #ccc;margin:8px 0;"
                f"padding:4px 12px;color:#444;font-style:italic'>"
                f"{item.get('quote_is', '')}</blockquote>"
                f"</div>"
            )
        parts.append("<br>")

    parts.append(
        f"<hr><p style='color:#999;font-size:0.8em'>"
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} "
        f"| <a href='https://www.reykjanesbaer.is/is/stjornsysla/stjornsyslan/fundargerdir'>Reykjanesbær</a> "
        f"| <a href='https://reykjavik.is/fundargerdir'>Reykjavík</a></p>"
    )

    return subject, "\n".join(parts)


# ── Email sender ──────────────────────────────────────────────────────────────

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Municipal Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Looking back {LOOKBACK_DAYS} days")
    print(f"{'='*60}\n")

    all_results = []

    for muni in MUNICIPALITIES:
        print(f"\n── {muni['name']} ──────────────────────────────")
        meetings = get_meetings(muni)
        print(f"  Found {len(meetings)} meetings on index page")

        recent = [m for m in meetings if is_recent(m["date"])]
        print(f"  {len(recent)} are within last {LOOKBACK_DAYS} days")

        priority = [m for m in recent if is_priority(m["committee"], muni["priority_committees"])]
        others   = [m for m in recent if not is_priority(m["committee"], muni["priority_committees"])]
        ordered  = priority + others

        for meeting in ordered:
            print(f"\n  {meeting['title'][:80]}")
            print(f"  {meeting['url']}")
            text  = get_meeting_text(meeting["url"])
            items = analyse_meeting(meeting["title"], meeting["url"], text)
            print(f"  → {len(items)} relevant item(s)")

            all_results.append({
                "municipality":  muni["name"],
                "meeting_title": meeting["title"],
                "url":           meeting["url"],
                "date_str":      meeting["date"].strftime("%d %b %Y") if meeting["date"] else "unknown date",
                "items":         items,
            })

    subject, html = build_email(all_results)
    print(f"\n{'='*60}")
    print(f"Subject: {subject}")
    print(f"{'='*60}")
    send_email(subject, html)


if __name__ == "__main__":
    main()
