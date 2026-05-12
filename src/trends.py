"""Job market trends analysis.

Workflow:
1. Pull positions collected over past N days from DB (filter by min_score, only suitable for candidate)
2. Python side aggregation: company frequency, skill frequency, locations, salary, seniority distribution
3. Feed aggregated data + sample JDs + candidate resume to Claude, generate narrative
4. Output markdown / HTML report

Design principles:
- Python does "arithmetic", Claude does "interpretation". Don't let Claude count, data 100% accurate.
- Narrative portion in report written by Claude, detailed tables rendered by Python.
- Stats Claude receives are pre-aggregated, not raw JDs, saves tokens and controls hallucination.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from .agent import load_prompt, make_client, render
from .config import Config
from .db import Job, JobStatus, session_scope
from .resume_reader import load_cached


# ============ Keyword Dictionary ============
# Not aiming for completeness, aiming for "frequently appearing in tech job JDs and can identify trends"
TECH_KEYWORDS = [
    # Languages
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Golang", "Rust",
    "C++", "C#", "Ruby", "Swift", "Kotlin", "Scala",
    # Frontend
    "React", "Vue", "Angular", "Next.js", "Svelte", "Tailwind",
    # Backend frameworks
    "Django", "Flask", "FastAPI", "Spring", "Express", "Rails", "NestJS",
    # Cloud / Infra
    "AWS", "GCP", "Azure", "Kubernetes", "Docker", "Terraform", "Ansible",
    "Linux", "CI/CD", "DevOps", "SRE",
    # Data
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "Snowflake",
    "BigQuery", "Spark", "Kafka", "Airflow", "dbt",
    # AI / ML
    "TensorFlow", "PyTorch", "JAX", "LangChain", "LlamaIndex", "OpenAI",
    "Claude", "Anthropic", "Gemini", "LLM", "RAG", "Vector Database",
    "Hugging Face", "Transformers", "Fine-tuning", "Embeddings",
    "Machine Learning", "Deep Learning", "Computer Vision", "NLP",
    "Reinforcement Learning", "MLOps",
    # Protocols / Architecture
    "GraphQL", "REST", "gRPC", "WebSocket", "Microservices", "Event-driven",
    "Distributed Systems",
    # Other
    "Git", "GitHub Actions", "Jenkins", "Datadog", "Prometheus", "Grafana",
    "OAuth", "OIDC", "Stripe API",
]

SENIORITY_PATTERNS: dict[str, list[str]] = {
    "intern":    [r"\bintern\b", r"\binternship\b"],
    "junior":    [r"\bjunior\b", r"\bjr\.?\b", r"\bentry[- ]level\b", r"\bnew\s+grad\b"],
    "mid":       [r"\bmid[- ]level\b"],
    "senior":    [r"\bsenior\b", r"\bsr\.?\b"],
    "staff":     [r"\bstaff\b"],
    "principal": [r"\bprincipal\b"],
    "lead":      [r"\btech(?:nical)?\s+lead\b", r"\blead\s+engineer\b"],
    "manager":   [r"\bengineering\s+manager\b", r"\bem\b", r"\bdirector\b", r"\bvp\b", r"\bhead\s+of\b"],
}


# ============ Extractors ============
def extract_skills(text: str, vocab: list[str] = TECH_KEYWORDS) -> set[str]:
    """Extract known skills from JD text. Use word-boundary to reduce false matches."""
    if not text:
        return set()
    found: set[str] = set()
    lower = text.lower()
    for kw in vocab:
        kl = kw.lower()
        # Multi-word: direct substring match; single word: word boundary match
        if " " in kl or "/" in kl or "." in kl or "+" in kl:
            if kl in lower:
                found.add(kw)
        else:
            if re.search(rf"\b{re.escape(kl)}\b", lower):
                found.add(kw)
    return found


def extract_seniority(title: str, description: str = "") -> str:
    text = f"{title or ''} {description or ''}".lower()
    for level, patterns in SENIORITY_PATTERNS.items():
        for p in patterns:
            if re.search(p, text):
                return level
    return "unspecified"


def extract_salary_range(salary: str | None) -> tuple[int, int] | None:
    """Parse '$120k - $150k' / '$120,000-$150,000' / '$80K to $100K USD' etc."""
    if not salary:
        return None
    nums: list[int] = []
    for m in re.finditer(r"\$?\s*(\d{1,3}(?:[,\.]\d{3})+|\d+\s*[Kk]?)", salary):
        raw = m.group(1).replace(",", "").replace(".", "").replace(" ", "")
        try:
            if raw.lower().endswith("k"):
                nums.append(int(raw[:-1]) * 1000)
            else:
                n = int(raw)
                # "120k" may be written as plain "120" - scale up if needed
                if n < 1000:
                    n *= 1000
                nums.append(n)
        except ValueError:
            continue
    nums = [n for n in nums if 10_000 <= n <= 1_500_000]
    if len(nums) >= 2:
        return min(nums), max(nums)
    return None


def normalize_location(loc: str | None) -> str:
    if not loc:
        return "unspecified"
    s = loc.strip()
    sl = s.lower()
    if "remote" in sl:
        return "Remote"
    if "hybrid" in sl:
        # Extract city
        city = re.split(r"[·,•]", s)[0].strip()
        return f"Hybrid - {city}" if city and "hybrid" not in city.lower() else "Hybrid"
    # Use first segment as city
    return re.split(r"[·,•]", s)[0].strip().title() or "unspecified"


# ============ Aggregation ============
def aggregate_stats(
    config: Config,
    days: int = 30,
    min_score: float | None = None,
) -> dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    with session_scope(config.path("db_path")) as session:
        stmt = select(Job).where(Job.created_at >= cutoff)
        stmt = stmt.where(Job.status != JobStatus.ARCHIVED.value)
        if min_score is not None:
            stmt = stmt.where(Job.match_score >= min_score)
        jobs = list(session.scalars(stmt).all())
        for j in jobs:
            session.expunge(j)

    n = len(jobs)
    stats: dict[str, Any] = {
        "period_days": days,
        "total_jobs": n,
        "min_score_filter": min_score,
        "generated_at": datetime.now().isoformat(timespec="minutes"),
    }
    if n == 0:
        return stats

    # Companies
    companies = Counter(j.company for j in jobs if j.company)
    stats["top_companies"] = companies.most_common(15)

    # Skills (extracted from title + description)
    skill_counts: Counter[str] = Counter()
    for j in jobs:
        text = f"{j.title or ''}\n{j.description or ''}"
        for s in extract_skills(text):
            skill_counts[s] += 1
    stats["top_skills"] = skill_counts.most_common(30)

    # Locations
    loc_counts: Counter[str] = Counter()
    for j in jobs:
        loc_counts[normalize_location(j.location)] += 1
    stats["top_locations"] = loc_counts.most_common(15)

    # Seniority levels
    sen_counts: Counter[str] = Counter()
    for j in jobs:
        sen_counts[extract_seniority(j.title or "", j.description or "")] += 1
    stats["seniority"] = dict(sen_counts.most_common())

    # Salary (only count if data exists)
    ranges = [extract_salary_range(j.salary) for j in jobs]
    ranges = [r for r in ranges if r]
    if ranges:
        lows = sorted(r[0] for r in ranges)
        highs = sorted(r[1] for r in ranges)
        stats["salary"] = {
            "samples": len(ranges),
            "median_low": lows[len(lows) // 2],
            "median_high": highs[len(highs) // 2],
            "p25_low": lows[len(lows) // 4],
            "p75_high": highs[3 * len(highs) // 4],
            "min": min(lows),
            "max": max(highs),
        }

    # Score distribution
    score_buckets: Counter[str] = Counter()
    for j in jobs:
        s = j.match_score
        if s is None:
            score_buckets["unscored"] += 1
        elif s >= 90:
            score_buckets["90+"] += 1
        elif s >= 80:
            score_buckets["80-89"] += 1
        elif s >= 70:
            score_buckets["70-79"] += 1
        elif s >= 60:
            score_buckets["60-69"] += 1
        else:
            score_buckets["<60"] += 1
    stats["score_distribution"] = dict(score_buckets.most_common())

    # Source distribution
    stats["by_source"] = dict(Counter(j.source for j in jobs).most_common())

    # Top jobs for narrative use + bottom table in report
    top_jobs_objs = sorted(
        [j for j in jobs if j.match_score is not None],
        key=lambda j: -(j.match_score or 0),
    )[:10]
    stats["top_jobs"] = [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "score": float(j.match_score or 0),
            "url": j.url,
            "location": j.location,
        }
        for j in top_jobs_objs
    ]

    # 6 sample JDs for Claude context
    samples = []
    for j in top_jobs_objs[:6]:
        samples.append({
            "title": j.title,
            "company": j.company,
            "score": float(j.match_score or 0),
            "description": (j.description or "")[:1200],
        })
    stats["_sample_jds"] = samples  # Underscore means not in final report table

    return stats


# ============ Claude narrative ============
def analyze_with_claude(
    config: Config,
    stats: dict[str, Any],
    resume_text: str,
) -> str:
    from .agent import llm_complete

    public_stats = {k: v for k, v in stats.items() if not k.startswith("_")}
    sample_jds_str = json.dumps(
        stats.get("_sample_jds", []), ensure_ascii=False, indent=2
    )
    prompt = render(
        load_prompt("trends"),
        resume=resume_text[:3500],
        period_days=stats["period_days"],
        stats_json=json.dumps(public_stats, ensure_ascii=False, indent=2, default=str),
        sample_jds=sample_jds_str,
    )
    # trends defaults to role "trends" (check config for provider)
    client, model_name, provider = make_client(config, "trends")
    return llm_complete(
        client,
        model_name,
        provider,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3500,
    )


# ============ Report Rendering ============
def render_markdown(stats: dict[str, Any], narrative: str) -> str:
    L: list[str] = []
    L.append(f"# Job Market Trends Report")
    L.append("")
    L.append(
        f"> {stats['generated_at']} · Past {stats['period_days']} days · "
        f"Analyzed {stats['total_jobs']} positions"
        + (f" · Score >= {stats['min_score_filter']:.0f} only" if stats.get("min_score_filter") else "")
    )
    L.append("")
    if stats["total_jobs"] == 0:
        L.append("⚠️ No positions matching criteria collected in this period. Run `collect` and `match` first.")
        return "\n".join(L)

    L.append(narrative)
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Data Details")
    L.append("")

    # Top companies
    L.append("### Top Employers")
    L.append("")
    L.append("| Company | Position Count |")
    L.append("|---|---:|")
    for c, n in stats.get("top_companies", []):
        L.append(f"| {c} | {n} |")
    L.append("")

    # Skills
    L.append("### Tech Stack Heat Top 30")
    L.append("")
    L.append("| Skill | Occurrences | Percentage |")
    L.append("|---|---:|---:|")
    total = stats["total_jobs"]
    for s, n in stats.get("top_skills", []):
        pct = 100 * n / total if total else 0
        L.append(f"| {s} | {n} | {pct:.0f}% |")
    L.append("")

    # Locations
    L.append("### Location Distribution")
    L.append("")
    L.append("| Location | Position Count |")
    L.append("|---|---:|")
    for loc, n in stats.get("top_locations", []):
        L.append(f"| {loc} | {n} |")
    L.append("")

    # Salary
    if stats.get("salary"):
        s = stats["salary"]
        L.append(f"### Salary Range (Sample size: {s['samples']})")
        L.append("")
        L.append(f"- Median starting salary: **${s['median_low']:,}**")
        L.append(f"- Median upper limit: **${s['median_high']:,}**")
        L.append(f"- P25 starting / P75 upper: ${s['p25_low']:,} / ${s['p75_high']:,}")
        L.append(f"- Overall range: ${s['min']:,} - ${s['max']:,}")
        L.append("")

    # Seniority
    L.append("### Seniority Distribution")
    L.append("")
    for level, count in stats.get("seniority", {}).items():
        L.append(f"- {level}: {count}")
    L.append("")

    # Source
    L.append("### Source Distribution")
    L.append("")
    for src, n in stats.get("by_source", {}).items():
        L.append(f"- {src}: {n}")
    L.append("")

    # Score distribution
    L.append("### Score Distribution")
    L.append("")
    for b, n in stats.get("score_distribution", {}).items():
        L.append(f"- {b}: {n}")
    L.append("")

    # Top jobs
    L.append("## Top 10 High Match Positions")
    L.append("")
    L.append("| ID | Score | Title | Company | Location |")
    L.append("|---:|---:|---|---|---|")
    for j in stats.get("top_jobs", []):
        L.append(
            f"| {j['id']} | {j['score']:.0f} | "
            f"[{j['title']}]({j['url']}) | {j['company']} | {j.get('location') or ''} |"
        )

    return "\n".join(L)


def send_trends_email(config: Config, html: str, subject: str | None = None) -> bool:
    """Reuse digest SMTP config to send trends report email."""
    from .digest import send_email as _send

    return _send(config, html, subject=subject or f"Market Trends Report - {datetime.now():%Y-%m-%d}")


def render_html(md_text: str, title: str) -> str:
    """Markdown -> HTML (simple wrapper, no external stylesheets)."""
    try:
        import markdown as md_lib
        body = md_lib.markdown(md_text, extensions=["tables", "fenced_code"])
    except Exception:
        # fallback: wrap in <pre>
        body = f"<pre>{md_text}</pre>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 880px; margin: 30px auto; padding: 0 20px; color: #1f2937; line-height: 1.6; }}
h1, h2, h3 {{ color: #111827; }}
h1 {{ border-bottom: 2px solid #111827; padding-bottom: 8px; }}
h2 {{ margin-top: 32px; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; }}
table {{ border-collapse: collapse; margin: 12px 0; }}
th, td {{ border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }}
th {{ background: #f9fafb; }}
blockquote {{ border-left: 3px solid #6b7280; margin: 0; padding: 4px 12px; color: #6b7280; }}
code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 3px; }}
</style>
</head><body>
{body}
</body></html>"""


# ============ Main Entry Point ============
def generate_report(
    config: Config,
    days: int = 30,
    min_score: float | None = None,
    formats: tuple[str, ...] = ("md",),
    send_email: bool = False,
) -> dict[str, Path]:
    """Generate trends report. Return {format: path}. When send_email=True, also send via SMTP."""
    stats = aggregate_stats(config, days=days, min_score=min_score)

    if stats["total_jobs"] == 0:
        narrative = "⚠️ No position data collected. Run `collect` + `match` first, then generate trends report."
    else:
        try:
            resume_text = load_cached(config.path("resume_dir"))
        except FileNotFoundError:
            resume_text = "(Resume not configured — recommendations will be generic)"
        narrative = analyze_with_claude(config, stats, resume_text)

    md_text = render_markdown(stats, narrative)
    outputs = config.path("outputs_dir")
    stamp = datetime.now().strftime("%Y%m%d")
    paths: dict[str, Path] = {}

    html_full = None
    if "md" in formats:
        p = outputs / f"trends_{stamp}.md"
        p.write_text(md_text, encoding="utf-8")
        paths["md"] = p
    if "html" in formats:
        title = f"Job Market Trends {stamp}"
        html_full = render_html(md_text, title)
        p = outputs / f"trends_{stamp}.html"
        p.write_text(html_full, encoding="utf-8")
        paths["html"] = p

    # Send email (reuse digest SMTP config)
    if send_email:
        if html_full is None:
            html_full = render_html(md_text, f"Job Market Trends {stamp}")
        send_trends_email(config, html_full)

    return paths
