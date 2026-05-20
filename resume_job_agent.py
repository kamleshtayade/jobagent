"""
Resume Job Agent
================
Reads a PDF resume from disk, extracts skills/experience via Claude,
scrapes LinkedIn + Indeed + Naukri via Apify, scores each job match,
and outputs a ranked Markdown report.

Usage:
    python resume_job_agent.py \
        --resume /path/to/resume.pdf \
        --anthropic-key sk-ant-... \
        --apify-key apify_api_... \
        [--locations "Bangalore,Pune,Remote"] \
        [--roles "Agentic AI Solution Architect,Engineering Manager"] \
        [--output job_matches.md]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pdfplumber
from anthropic import Anthropic
from apify_client import ApifyClient
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

# ─────────────────────────────────────────────
# 1.  PDF EXTRACTION
# ─────────────────────────────────────────────

def extract_resume_text(pdf_path: str) -> str:
    """Extract all text from a PDF file using pdfplumber."""
    path = Path(pdf_path)
    if not path.exists():
        console.print(f"[bold red]✗ File not found:[/] {pdf_path}")
        sys.exit(1)
    if path.suffix.lower() != ".pdf":
        console.print(f"[bold red]✗ Not a PDF file:[/] {pdf_path}")
        sys.exit(1)

    text_pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_pages.append(page_text)

    full_text = "\n\n".join(text_pages)
    if not full_text.strip():
        console.print("[bold red]✗ Could not extract text from PDF.[/] It may be image-based — try running OCR first.")
        sys.exit(1)

    console.print(f"[green]✓[/] Extracted {len(full_text)} chars from {len(text_pages)} page(s)")
    return full_text


# ─────────────────────────────────────────────
# 2.  RESUME PARSING VIA CLAUDE
# ─────────────────────────────────────────────

RESUME_PARSE_SYSTEM = """You are an expert technical recruiter and resume analyst.
Extract structured information from the resume text provided.
Respond ONLY with valid JSON — no markdown fences, no explanation."""

RESUME_PARSE_PROMPT = """Extract the following from this resume and return as JSON:

{
  "name": "full name",
  "title": "current job title",
  "years_experience": <integer total years>,
  "current_company": "company name",
  "location": "city, country",
  "tech_skills": ["list", "of", "technical", "skills"],
  "ai_ml_skills": ["specific AI/ML/GenAI skills"],
  "cloud_skills": ["cloud platforms and tools"],
  "leadership_skills": ["management and leadership capabilities"],
  "industries": ["industries worked in"],
  "key_achievements": ["top 5 quantified achievements"],
  "education": "highest degree + institution",
  "certifications": ["list of certifications"],
  "seniority_level": "junior|mid|senior|principal|director|vp",
  "target_roles": ["inferred target role titles based on experience"],
  "summary_for_matching": "2-3 sentence summary optimised for job matching"
}

RESUME TEXT:
---
{resume_text}
---"""


def parse_resume_with_claude(client: Anthropic, resume_text: str) -> dict:
    """Use Claude to extract structured profile from resume text."""
    console.print("[dim]Parsing resume with Claude...[/]")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=RESUME_PARSE_SYSTEM,
        messages=[{
            "role": "user",
            "content": RESUME_PARSE_PROMPT.format(resume_text=resume_text)
        }]
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if Claude adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        profile = json.loads(raw)
        console.print(f"[green]✓[/] Profile parsed: [bold]{profile.get('name', 'Unknown')}[/] — {profile.get('title', '')}")
        return profile
    except json.JSONDecodeError as e:
        console.print(f"[yellow]⚠ JSON parse failed, using raw text. Error: {e}[/]")
        return {"raw_text": raw, "name": "Unknown", "tech_skills": [], "ai_ml_skills": []}


# ─────────────────────────────────────────────
# 3.  APIFY JOB SCRAPING
# ─────────────────────────────────────────────

# Apify actor IDs for each platform
APIFY_ACTORS = {
    "linkedin": "curious_coder/linkedin-jobs-scraper",   # LinkedIn Jobs Scraper
    "indeed":   "misceres/indeed-scraper",                # Indeed Scraper
    "naukri":   "shashank_pathak/naukri-scraper",         # Naukri Scraper
}

def build_search_queries(profile: dict, roles: list[str], locations: list[str]) -> list[dict]:
    """Build search query combinations for scraping."""
    queries = []
    for role in roles:
        for location in locations:
            queries.append({"role": role, "location": location.strip()})
    return queries


def scrape_linkedin_jobs(apify: ApifyClient, queries: list[dict], max_per_query: int = 10) -> list[dict]:
    """Scrape LinkedIn jobs via Apify."""
    jobs = []
    for q in queries:
        try:
            console.print(f"  [dim]LinkedIn:[/] {q['role']} @ {q['location']}")
            run = apify.actor(APIFY_ACTORS["linkedin"]).call(run_input={
                "searchQueries": [f"{q['role']} {q['location']}"],
                "maxResults": max_per_query,
                "publishedAt": "r86400",   # last 48 hours
                "workType": ["remote", "hybrid", "onsite"],
            }, timeout_secs=120)
            for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
                item["_source"] = "linkedin"
                item["_query"] = q
                jobs.append(item)
        except Exception as e:
            console.print(f"  [yellow]⚠ LinkedIn scrape failed for '{q['role']}': {e}[/]")
    return jobs


def scrape_indeed_jobs(apify: ApifyClient, queries: list[dict], max_per_query: int = 10) -> list[dict]:
    """Scrape Indeed jobs via Apify."""
    jobs = []
    for q in queries:
        try:
            console.print(f"  [dim]Indeed:[/] {q['role']} @ {q['location']}")
            run = apify.actor(APIFY_ACTORS["indeed"]).call(run_input={
                "position": q["role"],
                "country": "IN",
                "location": q["location"],
                "maxItems": max_per_query,
                "startUrls": [],
            }, timeout_secs=120)
            for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
                item["_source"] = "indeed"
                item["_query"] = q
                jobs.append(item)
        except Exception as e:
            console.print(f"  [yellow]⚠ Indeed scrape failed for '{q['role']}': {e}[/]")
    return jobs


def scrape_naukri_jobs(apify: ApifyClient, queries: list[dict], max_per_query: int = 10) -> list[dict]:
    """Scrape Naukri jobs via Apify."""
    jobs = []
    for q in queries:
        try:
            console.print(f"  [dim]Naukri:[/] {q['role']} @ {q['location']}")
            run = apify.actor(APIFY_ACTORS["naukri"]).call(run_input={
                "keyword": q["role"],
                "location": q["location"],
                "maxResults": max_per_query,
                "experienceMin": 10,
                "experienceMax": 25,
            }, timeout_secs=120)
            for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
                item["_source"] = "naukri"
                item["_query"] = q
                jobs.append(item)
        except Exception as e:
            console.print(f"  [yellow]⚠ Naukri scrape failed for '{q['role']}': {e}[/]")
    return jobs


def normalise_job(raw: dict, source: str) -> dict:
    """Normalise scraped job data into a consistent schema across all sources."""
    if source == "linkedin":
        return {
            "title":       raw.get("title") or raw.get("jobTitle", ""),
            "company":     raw.get("companyName") or raw.get("company", ""),
            "location":    raw.get("location", ""),
            "description": raw.get("description") or raw.get("descriptionText", ""),
            "url":         raw.get("jobUrl") or raw.get("url", ""),
            "posted_at":   raw.get("postedAt") or raw.get("publishedAt", ""),
            "source":      "LinkedIn",
        }
    elif source == "indeed":
        return {
            "title":       raw.get("positionName") or raw.get("title", ""),
            "company":     raw.get("company", ""),
            "location":    raw.get("location", ""),
            "description": raw.get("description", ""),
            "url":         raw.get("url") or raw.get("externalApplyLink", ""),
            "posted_at":   raw.get("postedAt") or raw.get("date", ""),
            "source":      "Indeed",
        }
    elif source == "naukri":
        return {
            "title":       raw.get("title") or raw.get("jobTitle", ""),
            "company":     raw.get("companyName") or raw.get("company", ""),
            "location":    raw.get("location") or raw.get("jobLocation", ""),
            "description": raw.get("jobDescription") or raw.get("description", ""),
            "url":         raw.get("jdURL") or raw.get("url", ""),
            "posted_at":   raw.get("createdDate") or raw.get("postedOn", ""),
            "source":      "Naukri",
        }
    return {"title": "", "company": "", "location": "", "description": str(raw), "url": "", "source": source}


def scrape_all_jobs(apify: ApifyClient, queries: list[dict], max_per_query: int = 10) -> list[dict]:
    """Run all three scrapers and return normalised, deduplicated job list."""
    all_raw = []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task("Scraping LinkedIn...", total=None)
        all_raw += [(j, "linkedin") for j in scrape_linkedin_jobs(apify, queries, max_per_query)]

        progress.update(task, description="Scraping Indeed...")
        all_raw += [(j, "indeed")   for j in scrape_indeed_jobs(apify, queries, max_per_query)]

        progress.update(task, description="Scraping Naukri...")
        all_raw += [(j, "naukri")   for j in scrape_naukri_jobs(apify, queries, max_per_query)]

        progress.update(task, description="Done scraping.")

    normalised = [normalise_job(raw, src) for raw, src in all_raw]

    # Deduplicate by (company, title) pair
    seen = set()
    unique = []
    for job in normalised:
        key = f"{job['company'].lower().strip()}|{job['title'].lower().strip()}"
        if key and key not in seen:
            seen.add(key)
            unique.append(job)

    console.print(f"[green]✓[/] Scraped {len(all_raw)} raw listings → {len(unique)} unique jobs after dedup")
    return unique


# ─────────────────────────────────────────────
# 4.  JOB MATCHING VIA CLAUDE
# ─────────────────────────────────────────────

MATCH_SYSTEM = """You are a senior technical recruiter specialising in AI/ML and engineering leadership roles.
Your task is to score how well a candidate's profile matches a job description.
Be analytical, honest, and specific. Respond ONLY with valid JSON."""

MATCH_PROMPT = """Score this candidate against this job posting.

CANDIDATE PROFILE:
{profile_json}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Source: {source}
Description:
{description}

Return JSON with this exact structure:
{{
  "compatibility_score": <integer 0-100>,
  "score_breakdown": {{
    "tech_stack_match": <0-25>,
    "experience_level_match": <0-25>,
    "domain_context_match": <0-25>,
    "seniority_match": <0-25>
  }},
  "matched_keywords": ["keyword1", "keyword2"],
  "gap_keywords": ["missing1", "missing2"],
  "match_summary": "2 sentence honest assessment",
  "interview_angle": "1 sentence on how candidate should position themselves",
  "recommendation": "apply_now|apply_with_note|stretch|skip"
}}"""


def score_job_match(client: Anthropic, profile: dict, job: dict) -> dict:
    """Score a single job against the candidate profile using Claude."""
    desc = (job.get("description") or "")[:3000]  # cap at 3000 chars to save tokens

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=MATCH_SYSTEM,
            messages=[{
                "role": "user",
                "content": MATCH_PROMPT.format(
                    profile_json=json.dumps(profile, indent=2),
                    title=job.get("title", ""),
                    company=job.get("company", ""),
                    location=job.get("location", ""),
                    source=job.get("source", ""),
                    description=desc,
                )
            }]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        result = json.loads(raw)
        result["_job"] = job
        return result
    except Exception as e:
        return {
            "compatibility_score": 0,
            "score_breakdown": {},
            "matched_keywords": [],
            "gap_keywords": [],
            "match_summary": f"Scoring failed: {e}",
            "interview_angle": "",
            "recommendation": "skip",
            "_job": job,
        }


def score_all_jobs(client: Anthropic, profile: dict, jobs: list[dict]) -> list[dict]:
    """Score all jobs with progress display, rate-limiting between calls."""
    results = []
    total = len(jobs)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task(f"Scoring 0/{total} jobs...", total=total)
        for i, job in enumerate(jobs):
            progress.update(task, description=f"Scoring {i+1}/{total}: {job.get('company','')} — {job.get('title','')[:40]}")
            score = score_job_match(client, profile, job)
            results.append(score)
            progress.advance(task)
            time.sleep(0.3)   # gentle rate limiting

    results.sort(key=lambda x: x.get("compatibility_score", 0), reverse=True)
    console.print(f"[green]✓[/] Scored {len(results)} jobs")
    return results


# ─────────────────────────────────────────────
# 5.  REPORT GENERATION
# ─────────────────────────────────────────────

RECOMMENDATION_EMOJI = {
    "apply_now":        "🟢",
    "apply_with_note":  "🟡",
    "stretch":          "🟠",
    "skip":             "🔴",
}

RECOMMENDATION_LABEL = {
    "apply_now":        "Apply now",
    "apply_with_note":  "Apply with positioning note",
    "stretch":          "Stretch role",
    "skip":             "Skip",
}


def score_bar(score: int, width: int = 20) -> str:
    filled = int(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def generate_markdown_report(profile: dict, scored_jobs: list[dict], locations: str, roles: str) -> str:
    now = datetime.now().strftime("%d %B %Y, %H:%M")

    apply_now    = [j for j in scored_jobs if j.get("recommendation") == "apply_now"]
    apply_note   = [j for j in scored_jobs if j.get("recommendation") == "apply_with_note"]
    stretch      = [j for j in scored_jobs if j.get("recommendation") == "stretch"]
    skip         = [j for j in scored_jobs if j.get("recommendation") == "skip"]

    avg_score = sum(j.get("compatibility_score", 0) for j in scored_jobs) / max(len(scored_jobs), 1)

    lines = []

    # ── Header ──────────────────────────────────
    lines += [
        f"# Job Match Report — {profile.get('name', 'Candidate')}",
        f"> Generated: {now}  |  Locations: {locations}  |  Roles: {roles}",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total jobs analysed | {len(scored_jobs)} |",
        f"| Apply now (≥80) | {len(apply_now)} |",
        f"| Apply with note (60–79) | {len(apply_note)} |",
        f"| Stretch (<60) | {len(stretch)} |",
        f"| Skipped | {len(skip)} |",
        f"| Average compatibility score | {avg_score:.0f}/100 |",
        f"| Top score | {scored_jobs[0].get('compatibility_score', 0) if scored_jobs else 0}/100 |",
        "",
        "---",
        "",
        "## Candidate Profile Snapshot",
        "",
        f"- **Name:** {profile.get('name', 'N/A')}",
        f"- **Title:** {profile.get('title', 'N/A')}",
        f"- **Experience:** {profile.get('years_experience', 'N/A')} years",
        f"- **Current company:** {profile.get('current_company', 'N/A')}",
        f"- **Location:** {profile.get('location', 'N/A')}",
        f"- **Seniority level:** {profile.get('seniority_level', 'N/A')}",
        f"- **AI/ML skills:** {', '.join(profile.get('ai_ml_skills', []))}",
        f"- **Cloud skills:** {', '.join(profile.get('cloud_skills', []))}",
        f"- **Summary:** {profile.get('summary_for_matching', '')}",
        "",
        "---",
        "",
        "## Ranked Job Matches",
        "",
    ]

    # ── Individual job entries ───────────────────
    for rank, job in enumerate(scored_jobs, 1):
        j = job.get("_job", {})
        score = job.get("compatibility_score", 0)
        rec = job.get("recommendation", "skip")
        emoji = RECOMMENDATION_EMOJI.get(rec, "⚪")
        label = RECOMMENDATION_LABEL.get(rec, rec)
        breakdown = job.get("score_breakdown", {})

        lines += [
            f"### {rank}. {j.get('company', 'Unknown')} — {j.get('title', 'Unknown')}",
            "",
            f"**Score:** `{score}/100`  `{score_bar(score)}`  {emoji} **{label}**",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Source | {j.get('source', 'N/A')} |",
            f"| Location | {j.get('location', 'N/A')} |",
            f"| Posted | {j.get('posted_at', 'N/A')} |",
            f"| Apply | {j.get('url', 'N/A')} |",
            "",
            "**Score breakdown:**",
            "",
            f"| Dimension | Score |",
            f"|-----------|-------|",
            f"| Tech stack match | {breakdown.get('tech_stack_match', 0)}/25 |",
            f"| Experience level | {breakdown.get('experience_level_match', 0)}/25 |",
            f"| Domain context | {breakdown.get('domain_context_match', 0)}/25 |",
            f"| Seniority match | {breakdown.get('seniority_match', 0)}/25 |",
            "",
        ]

        matched = job.get("matched_keywords", [])
        gaps = job.get("gap_keywords", [])
        if matched:
            lines.append(f"**Matched keywords:** {', '.join(f'`{k}`' for k in matched)}")
            lines.append("")
        if gaps:
            lines.append(f"**Gaps to address:** {', '.join(f'`{k}`' for k in gaps)}")
            lines.append("")

        lines += [
            f"**Assessment:** {job.get('match_summary', '')}",
            "",
            f"**How to position yourself:** {job.get('interview_angle', '')}",
            "",
            "---",
            "",
        ]

    # ── Priority action list ─────────────────────
    lines += [
        "## Priority Action List",
        "",
        "### 🟢 Apply Now",
        "",
    ]
    if apply_now:
        for j in apply_now:
            jd = j.get("_job", {})
            lines.append(f"- **{jd.get('company')} — {jd.get('title')}** ({j['compatibility_score']}/100) → {jd.get('url', 'N/A')}")
    else:
        lines.append("- None in this batch")

    lines += ["", "### 🟡 Apply with positioning note", ""]
    if apply_note:
        for j in apply_note:
            jd = j.get("_job", {})
            lines.append(f"- **{jd.get('company')} — {jd.get('title')}** ({j['compatibility_score']}/100) → {jd.get('url', 'N/A')}")
    else:
        lines.append("- None in this batch")

    lines += ["", "---", "", f"*Report generated by Resume Job Agent — {now}*", ""]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 6.  RICH CONSOLE SUMMARY TABLE
# ─────────────────────────────────────────────

def print_summary_table(scored_jobs: list[dict]) -> None:
    table = Table(title="Top Job Matches", show_lines=True)
    table.add_column("#",          style="dim",    width=4)
    table.add_column("Company",    style="bold",   width=22)
    table.add_column("Title",                      width=35)
    table.add_column("Score",      style="cyan",   width=8)
    table.add_column("Action",                     width=20)
    table.add_column("Source",     style="dim",    width=10)

    colours = {"apply_now": "green", "apply_with_note": "yellow", "stretch": "dark_orange", "skip": "red"}

    for i, job in enumerate(scored_jobs[:15], 1):
        j = job.get("_job", {})
        rec = job.get("recommendation", "skip")
        colour = colours.get(rec, "white")
        label = RECOMMENDATION_LABEL.get(rec, rec)
        table.add_row(
            str(i),
            j.get("company", "")[:22],
            j.get("title", "")[:35],
            f"{job.get('compatibility_score', 0)}/100",
            f"[{colour}]{label}[/{colour}]",
            j.get("source", ""),
        )

    console.print(table)


# ─────────────────────────────────────────────
# 7.  ENTRY POINT
# ─────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Load YAML config file if available."""
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        console.print("[yellow]⚠ PyYAML not installed. Run: pip install pyyaml --break-system-packages[/]")
        return {}
    except FileNotFoundError:
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="Resume Job Agent — analyse resume + scrape + match jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using CLI flags:
  python resume_job_agent.py \\
      --resume /home/kamlesh/resume.pdf \\
      --anthropic-key sk-ant-XXX \\
      --apify-key apify_api_XXX

  # Using config file (recommended):
  python resume_job_agent.py --config config.yaml --resume /home/kamlesh/resume.pdf
        """
    )
    parser.add_argument("--resume",        required=True,  help="Full path to PDF resume file")
    parser.add_argument("--config",        default=None,   help="Path to YAML config file (see config.example.yaml)")
    parser.add_argument("--anthropic-key", default=None,   help="Anthropic API key (overrides config)")
    parser.add_argument("--apify-key",     default=None,   help="Apify API token (overrides config)")
    parser.add_argument("--locations",     default=None,
                        help="Comma-separated locations (default: Bangalore,Pune,Remote)")
    parser.add_argument("--roles",         default=None,
                        help="Comma-separated target role titles")
    parser.add_argument("--max-per-query", type=int, default=None,
                        help="Max jobs per query per platform (default: 10)")
    parser.add_argument("--output",        default=None,
                        help="Output Markdown report filename (default: job_matches.md)")
    parser.add_argument("--min-score",     type=int, default=None,
                        help="Only include jobs >= this score in report (default: 0)")

    args = parser.parse_args()

    # Merge config file → CLI flags (CLI wins)
    cfg = load_config(args.config) if args.config else {}

    anthropic_key = args.anthropic_key or cfg.get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY")
    apify_key     = args.apify_key     or cfg.get("apify_key")     or os.environ.get("APIFY_API_TOKEN")
    max_per_query = args.max_per_query or cfg.get("max_per_query", 10)
    min_score     = args.min_score     if args.min_score is not None else cfg.get("min_score", 0)
    output        = args.output        or cfg.get("output", "job_matches.md")

    cfg_locations = cfg.get("locations", [])
    cfg_roles     = cfg.get("roles", [])
    locations_str = args.locations or (",".join(cfg_locations) if cfg_locations else "Bangalore,Pune,Remote")
    roles_str     = args.roles     or (",".join(cfg_roles)     if cfg_roles     else "Agentic AI Solution Architect,Engineering Manager AI Platform")

    if not anthropic_key:
        console.print("[bold red]✗ Anthropic API key required.[/] Pass --anthropic-key, set in config.yaml, or export ANTHROPIC_API_KEY=...")
        sys.exit(1)
    if not apify_key:
        console.print("[bold red]✗ Apify API key required.[/] Pass --apify-key, set in config.yaml, or export APIFY_API_TOKEN=...")
        sys.exit(1)

    # Patch args namespace for rest of function
    args.anthropic_key = anthropic_key
    args.apify_key     = apify_key
    args.max_per_query = max_per_query
    args.min_score     = min_score
    args.output        = output
    args.locations     = locations_str
    args.roles         = roles_str

    console.print(Panel.fit(
        "[bold cyan]Resume Job Agent[/]\n"
        "Powered by Claude + Apify  |  LinkedIn · Indeed · Naukri",
        border_style="cyan"
    ))

    locations = [loc.strip() for loc in args.locations.split(",")]
    roles     = [role.strip() for role in args.roles.split(",")]

    console.print(f"\n[bold]Resume:[/] {args.resume}")
    console.print(f"[bold]Locations:[/] {', '.join(locations)}")
    console.print(f"[bold]Roles:[/] {', '.join(roles)}\n")

    # ── Step 1: Extract PDF ──────────────────────
    console.print("[bold]Step 1/5[/] Extracting resume from PDF...")
    resume_text = extract_resume_text(args.resume)

    # ── Step 2: Parse resume ────────────────────
    console.print("\n[bold]Step 2/5[/] Parsing resume with Claude...")
    claude = Anthropic(api_key=args.anthropic_key)
    profile = parse_resume_with_claude(claude, resume_text)

    # ── Step 3: Scrape jobs ──────────────────────
    console.print("\n[bold]Step 3/5[/] Scraping jobs from LinkedIn, Indeed, Naukri...")
    apify = ApifyClient(token=args.apify_key)
    queries = build_search_queries(profile, roles, locations)
    console.print(f"  Running {len(queries)} search queries × 3 platforms...")
    jobs = scrape_all_jobs(apify, queries, max_per_query=args.max_per_query)

    if not jobs:
        console.print("[bold yellow]⚠ No jobs scraped.[/] Check your Apify actor IDs and API key.")
        console.print("  Tip: Run with a lower --max-per-query or verify actor names at apify.com/store")
        sys.exit(0)

    # ── Step 4: Score matches ────────────────────
    console.print(f"\n[bold]Step 4/5[/] Scoring {len(jobs)} jobs against your profile...")
    scored = score_all_jobs(claude, profile, jobs)

    # Filter by min score
    if args.min_score > 0:
        before = len(scored)
        scored = [j for j in scored if j.get("compatibility_score", 0) >= args.min_score]
        console.print(f"  Filtered to {len(scored)} jobs with score ≥ {args.min_score} (from {before})")

    # ── Step 5: Generate report ──────────────────
    console.print(f"\n[bold]Step 5/5[/] Generating report...")
    print_summary_table(scored)

    report = generate_markdown_report(profile, scored, args.locations, args.roles)
    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")

    console.print(f"\n[bold green]✓ Done![/] Report saved to [underline]{output_path.resolve()}[/]")
    console.print(f"  {len([j for j in scored if j.get('recommendation')=='apply_now'])} roles to apply now  |  "
                  f"{len([j for j in scored if j.get('recommendation')=='apply_with_note'])} with positioning note\n")


if __name__ == "__main__":
    main()