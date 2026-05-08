# Municipal Meeting Monitor 🏗

Automatically scans Reykjanesbær and Reykjavík meeting minutes every week and emails a digest of new construction projects, tenders, and planning changes.

**Sends to:** vidir@istak.is  
**Schedule:** Every Monday at 09:00 Reykjavík time  

---

## What it does

Each week it:
1. Fetches the fundargerðir index for both municipalities
2. Finds all meetings published in the last 8 days
3. Prioritises planning, building permit, and procurement committees
4. Sends each meeting's text to Claude (AI) to extract relevant items
5. Emails a formatted HTML digest with English summaries + original Icelandic quotes

**Item types detected:** construction projects, tenders/procurement, zoning changes, building permits, infrastructure works, land/lot allocations

---

## One-time setup (15 minutes)

### 1. Create a GitHub repository

```bash
git init municipal-monitor
cd municipal-monitor
# copy all files from this zip into the folder
git add .
git commit -m "Initial setup"
# create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/municipal-monitor.git
git push -u origin main
```

### 2. Get your API keys

**Anthropic API key:**
- Go to https://console.anthropic.com
- Create an API key

**Gmail App Password** (for sending email):
- Go to your Google Account → Security → 2-Step Verification → App passwords
- Create a new app password (name it "Municipal Monitor")
- You get a 16-character password — save it

### 3. Add secrets to GitHub

In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret**

Add these three secrets:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_USER` | The Gmail address sending the email (e.g. yourname@gmail.com) |
| `GMAIL_APP_PASSWORD` | The 16-character app password from step 2 |

### 4. Enable Actions

Go to your repo → **Actions** tab → click "I understand my workflows, go ahead and enable them"

### 5. Test it

Go to **Actions → Weekly Municipal Monitor → Run workflow** to trigger it manually right now.

---

## Adding more municipalities

In `scraper.py`, add a new entry to the `MUNICIPALITIES` list:

```python
{
    "name": "Hafnarfjörður",
    "index_url": "https://www.hafnarfjordur.is/...",
    "base_url":  "https://www.hafnarfjordur.is",
    "type":      "reykjavik",   # use "reykjavik" for table-based layouts
    "priority_committees": ["umhverfis-og-skipulagsrad", ...],
},
```

---

## Email format

Each email contains:
- **Type badge** (Construction / Tender / Zoning / Permit / Infrastructure / Land)
- **Status** (Approved ✅ / Rejected ❌ / Under Review 🔍 / Advertised 📢)
- **English summary** (2-3 sentences)
- **Original Icelandic quote** in blockquote

---

## Files

```
municipal-monitor/
├── scraper.py                          # main script
├── requirements.txt                    # Python dependencies
├── README.md                           # this file
└── .github/
    └── workflows/
        └── weekly-monitor.yml          # GitHub Actions schedule
```
