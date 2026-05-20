# Resume Job Agent 🤖

Analyses your PDF resume, scrapes LinkedIn + Indeed + Naukri via Apify,
scores every job against your profile using Claude, and outputs a ranked
Markdown report — all from a single command.

---

## Architecture

```
PDF resume (disk)
      │
      ▼
pdfplumber ──► extracted text
      │
      ▼
Claude API ──► structured profile JSON
      │                   │
      ▼                   ▼
Apify Actors ◄────────── search queries
  ├── LinkedIn Jobs Scraper
  ├── Indeed Scraper
  └── Naukri Scraper
      │
      ▼
Normalised + deduped job list
      │
      ▼
Claude API ──► score each job (0-100) + gaps + recommendation
      │
      ▼
job_matches.md  (ranked report with apply links)
```

---

## Setup

### 1. Install dependencies

```bash
pip install pdfplumber anthropic apify-client rich pyyaml
```

### 2. Get your API keys

| Key | Where to get it |
|-----|-----------------|
| Anthropic | https://console.anthropic.com/settings/keys |
| Apify | https://console.apify.com/account/integrations |

### 3. Create config file

```bash
cp config.example.yaml config.yaml
# Edit config.yaml and fill in your keys
```

---

## Usage

### Option A — Using config file (recommended)

```bash
python resume_job_agent.py \
    --config config.yaml \
    --resume /path/to/your_resume.pdf
```

### Option B — Using CLI flags

```bash
python resume_job_agent.py \
    --resume /path/to/your_resume.pdf \
    --anthropic-key sk-ant-YOUR_KEY \
    --apify-key apify_api_YOUR_KEY \
    --locations "Bangalore,Pune,Remote" \
    --roles "Agentic AI Solution Architect,Engineering Manager AI Platform" \
    --output job_matches.md \
    --min-score 60
```

### Option C — Using environment variables

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export APIFY_API_TOKEN=apify_api_...

python resume_job_agent.py \
    --resume /path/to/resume.pdf \
    --config config.yaml
```

---

## Output

The agent produces `job_matches.md` with:

- **Executive summary** — counts by recommendation tier
- **Candidate profile snapshot** — extracted from your resume
- **Ranked job entries** — each with:
  - Compatibility score (0–100) with visual score bar
  - Score breakdown across 4 dimensions (tech stack, experience, domain, seniority)
  - Matched keywords
  - Gaps to address
  - Interview positioning angle
  - Direct apply link
- **Priority action list** — grouped by recommendation

### Recommendation tiers

| Tier | Score range | Meaning |
|------|-------------|---------|
| 🟢 Apply now | 80–100 | Strong match, apply immediately |
| 🟡 Apply with note | 60–79 | Good match, tailor your positioning |
| 🟠 Stretch | 40–59 | Partial match, worth a shot |
| 🔴 Skip | < 40 | Poor fit, move on |

---

## Apify Actors Used

| Platform | Actor | Notes |
|----------|-------|-------|
| LinkedIn | `curious_coder/linkedin-jobs-scraper` | Searches by keyword + location |
| Indeed | `misceres/indeed-scraper` | India (`country: IN`) |
| Naukri | `shashank_pathak/naukri-scraper` | Filters by experience range |

To use different actors, update the `APIFY_ACTORS` dict at the top of `resume_job_agent.py`.

---

## Troubleshooting

**"No jobs scraped"**
- Verify your Apify token at https://console.apify.com
- Check actor IDs still exist: https://apify.com/store
- Apify free tier has limited monthly compute — check your usage

**"JSON parse failed" warning**
- Usually harmless — the agent falls back gracefully
- If recurring, check your Anthropic API key

**PDF extraction returns empty text**
- Your PDF may be image-scanned — run OCR first (e.g. Adobe Acrobat or `ocrmypdf`)
- Or check if the PDF opens normally in a viewer

**Rate limiting from Anthropic**
- Increase the `time.sleep(0.3)` in `score_all_jobs()` if you hit 429s