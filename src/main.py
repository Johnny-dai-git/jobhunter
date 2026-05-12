"""JobHunter CLI entry point."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from .auth import login_and_save
from .collect import collect_all, PLATFORMS
from .config import Config
from .cover_letter import write_cover_letter
from .db import Job, JobStatus, init_db, session_scope
from .digest import run_digest
from .matcher import score_pending
from .profile_analyzer import analyze_profile as _analyze_profile, load_profile, save_profile
from .resume_reader import load_cached, parse_and_cache
from .tailor import tailor_for_job
from .tracker import mark_applied as _mark_applied
from .trends import generate_report as generate_trends_report


console = Console()


def _load_config() -> Config:
    try:
        return Config.load()
    except Exception as e:
        console.print(f"[red]Config load failed:[/red] {e}")
        sys.exit(1)


@click.group()
def cli():
    """JobHunter — personal job search agent."""


@cli.command()
def init():
    """Initialize database and directory structure."""
    config = _load_config()
    db_path = config.path("db_path")
    init_db(db_path)
    config.path("resume_dir")
    config.path("jobs_dir")
    config.path("outputs_dir")
    materials_dir = config.path("materials_dir")  # Auto-create

    console.print(f"[green]✓[/green] Database created: {db_path}")
    console.print(f"")
    console.print(f"[bold]── Resume Library ──[/bold]")
    console.print(f"[green]✓[/green] Directory: {config.path('resume_dir')}")
    console.print(f"    └ Store different resume versions (PDF / DOCX / MD / TXT)")
    console.print(f"")
    console.print(f"[bold]── Materials Library ──[/bold]")
    console.print(f"[green]✓[/green] Directory: {materials_dir}")
    console.print(f"    └ Store personal background materials: articles, papers, project docs, portfolio, etc.")
    console.print(f"    └ Supported: PDF / DOCX / MD / TXT")
    console.print(f"    └ Auto-read during tailor, helps Claude better extract your highlights")


@cli.command("parse-resume")
def parse_resume():
    """Read resume and cache as plain text."""
    config = _load_config()
    resume_dir = config.path("resume_dir")
    src, text = parse_and_cache(resume_dir)
    console.print(f"[green]✓[/green] Parsed: {src.name} ({len(text)} characters)")


@cli.command("analyze-profile")
@click.option("--force", is_flag=True, help="Overwrite existing _profile.json and re-analyze")
@click.option("--job-type", "job_type", default="full-time",
              type=click.Choice(["full-time", "internship", "both"], case_sensitive=False),
              help="Job type: full-time | internship | both")
def analyze_profile_cmd(force, job_type):
    """Let model read your resume, infer Top-10 capability directions + real search titles.

    First define 10 primaries, then collect phase uses aliases + broader_terms for fuzzy expansion.
    Results stored in data/resume/_profile.json.

    Examples:
      analyze-profile --force --job-type internship   # Look specifically for internships
      analyze-profile --force --job-type both         # Look for both full-time and internship
    """
    jt_map = {"full-time": ["Full-time"], "internship": ["Internship"], "both": ["Full-time","Internship"]}
    job_types = jt_map.get(job_type.lower(), ["Full-time"])

    config = _load_config()
    existing = load_profile(config)
    if existing and not force:
        console.print("[yellow]Profile cache exists, add --force to force re-analysis.[/yellow]")
        _print_profile(existing)
        return

    console.print(f"[cyan]→ Analyzing profile (job_types={job_types})...[/cyan]")
    profile = _analyze_profile(config, job_types=job_types)
    path = save_profile(config, profile)
    console.print(f"[green]✓[/green] Saved to {path}\n")
    _print_profile(profile)


def _print_profile(profile):
    """Print profile results for user (new version: three-dimension scoring + aliases + broader_terms)."""
    console.print(f"[bold]Core positioning:[/bold] {profile.summary}\n")

    tbl = Table(
        title="Top 10 optimal submission positions (by composite descending)",
        show_lines=True,
    )
    tbl.add_column("#", style="bold")
    tbl.add_column("Primary + Aliases (for search)")
    tbl.add_column("Dir", style="dim")
    tbl.add_column("M/C/A→Comp", justify="center", style="bold cyan")
    tbl.add_column("Why apply for this", style="dim")
    for i, p in enumerate(profile.top_10_positions, 1):
        s = p.scores
        why = "\n".join(f"• {w}" for w in p.why_this_position[:3])
        titles_block = f"[bold]{p.title}[/bold]"
        if p.aliases:
            titles_block += "\n[dim]aka:[/dim]\n  " + "\n  ".join(f"• {a}" for a in p.aliases)
        if p.broader_terms:
            titles_block += "\n[dim]hidden under:[/dim]\n  " + "\n  ".join(f"• {b}" for b in p.broader_terms)
        tbl.add_row(
            str(i),
            titles_block,
            p.direction[:8],
            f"{s.market_demand}/{s.competition}/{s.user_advantage}\n→ [yellow]{s.composite}[/yellow]",
            why,
        )
    console.print(tbl)
    console.print(
        "[dim]M = market_demand  C = competition (lower is better)  "
        "A = user_advantage  Comp = composite[/dim]"
    )

    # Summary of search terms for collector (Top-10 primary + aliases + broader_terms fuzzy expansion)
    all_terms = profile.search_titles(
        include_aliases=True, include_broader=True, limit=40
    )
    console.print(
        f"\n[bold]Collect will actually use {len(all_terms)} search terms "
        f"(Top-10 primary + aliases + broader_terms):[/bold]"
    )
    for t in all_terms:
        console.print(f"  • {t}")

    console.print(
        f"\n[bold]Target locations:[/bold] " + ", ".join(profile.target_locations)
    )

    console.print("\n[bold]LinkedIn direct links (primary title):[/bold]")
    for p in profile.top_10_positions:
        console.print(f"  • [link]{p.linkedin_search_url}[/link]")

    # Regional company recommendations
    if profile.recommended_companies:
        console.print(
            "\n[bold green]Target company list (by region, background match + active expansion):[/bold green]"
        )
        for region_label, companies in profile.recommended_companies.regions():
            if not companies:
                continue
            tbl = Table(title=f"🌍 {region_label} ({len(companies)} companies)", show_lines=True)
            tbl.add_column("Company", style="bold")
            tbl.add_column("Why fit", style="dim", overflow="fold")
            tbl.add_column("Expansion/hiring signal", overflow="fold")
            tbl.add_column("Example roles", style="cyan", overflow="fold")
            for c in companies:
                tbl.add_row(
                    c.name,
                    c.why_fit,
                    c.hiring_signal,
                    "\n".join(c.example_roles),
                )
            console.print(tbl)


@cli.command()
@click.option(
    "--platform",
    type=click.Choice(PLATFORMS),
    required=True,
    help="Platform to log in to",
)
@click.option("--timeout", type=int, default=300, help="Maximum seconds to wait for login")
def login(platform, timeout):
    """Open browser to let you manually log in to a platform, then save cookies."""
    config = _load_config()
    p = login_and_save(config, platform, timeout_sec=timeout)
    console.print(f"[green]✓[/green] {platform} cookies saved to {p}")


@cli.command()
@click.option(
    "--platform",
    type=click.Choice(PLATFORMS + ["all"]),
    default="all",
    help="Which platform to collect from",
)
def collect(platform):
    """Scrape positions from recruiting platforms, auto-deduplicate and store."""
    config = _load_config()
    plats = PLATFORMS if platform == "all" else [platform]
    stats = collect_all(config, plats)
    console.print(
        f"\n[green]✓[/green] Total {stats['total_new']} new positions "
        f"(deduplicated {stats['total_seen']} seen)"
    )
    for plat, s in stats["by_platform"].items():
        console.print(f"  {plat}: {s['new']} new, {s['duplicate']} duplicate")


@cli.command("add-job")
@click.option("--url", default="", help="Position link (optional, for deduplication and application)")
@click.option("--jd-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="File containing JD (PDF/DOCX/MD/TXT)")
@click.option("--jd", default=None, help="Paste JD text directly")
@click.option("--no-match", is_flag=True, help="Skip auto-scoring, only store")
def add_job(url, jd_file, jd, no_match):
    """Manually add position: paste JD text or upload file, DeepSeek auto-extract fields + score and store.

    Examples:
      add-job --url https://... --jd "We are looking for..."
      add-job --jd-file ~/Downloads/jd.pdf --url https://...
      echo "JD text" | add-job  (read from stdin)
    """
    from .manual_add import ManualJobInput, add_job_from_file, add_job_from_text

    config = _load_config()

    # 获取 JD 文本
    if jd_file:
        job, is_new = add_job_from_file(
            config, Path(jd_file), url=url, run_matcher=not no_match
        )
    else:
        raw_text = jd
        if not raw_text:
            # 从 stdin 读取
            if not sys.stdin.isatty():
                raw_text = sys.stdin.read().strip()
            else:
                console.print("[yellow]请粘贴 JD 文本 (输入完后按 Ctrl+D):[/yellow]")
                raw_text = sys.stdin.read().strip()
        if not raw_text:
            console.print("[red]错误: 请通过 --jd、--jd-file 或 stdin 提供 JD 内容[/red]")
            raise SystemExit(1)
        inp = ManualJobInput(raw_text=raw_text, url=url)
        job, is_new = add_job_from_text(config, inp, run_matcher=not no_match)

    if is_new:
        score_str = f"  match={job.match_score:.0f}" if job.match_score is not None else ""
        console.print(f"[green]✓[/green] Stored #{job.id}: {job.title} @ {job.company}{score_str}")
    else:
        console.print(f"[yellow]Skip[/yellow] Position already exists #{job.id}: {job.title} @ {job.company}")


@cli.command()
@click.option("--limit", type=int, default=None)
def match(limit):
    """Call Claude to score all unscored positions."""
    config = _load_config()
    resume_dir = config.path("resume_dir")
    try:
        resume_text = load_cached(resume_dir)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        console.print("First put resume in data/resume/, then run `parse-resume`")
        sys.exit(1)

    console.print("[cyan]Scoring...[/cyan]")
    results = score_pending(config, resume_text, limit=limit)
    if not results:
        console.print("[yellow]No unscored positions.[/yellow]")
        return

    table = Table(title=f"Scoring results ({len(results)} positions)")
    table.add_column("ID", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Summary")
    for job_id, r in sorted(results, key=lambda x: -x[1].score):
        color = "green" if r.score >= 75 else ("yellow" if r.score >= 60 else "red")
        table.add_row(str(job_id), f"[{color}]{r.score:.0f}[/{color}]", r.summary)
    console.print(table)


@cli.command("list")
@click.option(
    "--status",
    default="active",
    type=click.Choice(["all", "active", "new", "scored", "applied", "archived"]),
)
@click.option("--min-score", type=float, default=None)
def list_jobs(status, min_score):
    """View tracking table."""
    config = _load_config()
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        stmt = select(Job)
        if status == "active":
            stmt = stmt.where(Job.status != JobStatus.ARCHIVED.value)
        elif status != "all":
            stmt = stmt.where(Job.status == status)
        if min_score is not None:
            stmt = stmt.where(Job.match_score >= min_score)
        stmt = stmt.order_by(Job.match_score.desc().nulls_last(), Job.id.desc())
        jobs = session.scalars(stmt).all()

    if not jobs:
        console.print("[yellow]No matching positions.[/yellow]")
        return

    table = Table(title=f"Position list ({len(jobs)})")
    table.add_column("ID", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    for j in jobs:
        score_str = f"{j.match_score:.0f}" if j.match_score is not None else "-"
        color = (
            "green" if (j.match_score or 0) >= 75
            else ("yellow" if (j.match_score or 0) >= 60 else "white")
        )
        table.add_row(
            str(j.id),
            f"[{color}]{score_str}[/{color}]",
            j.status,
            (j.title or "")[:40],
            (j.company or "")[:25],
            (j.location or "")[:25],
        )
    console.print(table)


@cli.command()
@click.option("--job-id", type=int, required=True)
@click.option("--name", default="Candidate")
@click.option("--no-cover", is_flag=True)
def tailor(job_id, name, no_cover):
    """Generate customized resume and cover letter for a position."""
    config = _load_config()
    resume_text = load_cached(config.path("resume_dir"))

    console.print("[cyan]→ Generating customized resume...[/cyan]")
    resume_path = tailor_for_job(config, resume_text, job_id, candidate_name=name)
    console.print(f"[green]✓[/green] Resume: {resume_path}")

    if not no_cover:
        console.print("[cyan]→ Generating cover letter...[/cyan]")
        cover_path = write_cover_letter(config, resume_text, job_id)
        console.print(f"[green]✓[/green] Cover letter: {cover_path}")


@cli.command("mark-applied")
@click.option("--job-id", type=int, required=True)
@click.option("--note", default=None, help="Optional note, e.g. 'submitted via company website'")
def mark_applied(job_id, note):
    """After you manually apply, tell agent: this position applied. Only status record, no action triggered."""
    config = _load_config()
    _mark_applied(config, job_id, note)
    console.print(f"[green]✓[/green] #{job_id} marked as applied")


@cli.command()
def digest():
    """Generate daily Top-N digest HTML, optionally send email."""
    config = _load_config()
    p = run_digest(config)
    console.print(f"[green]✓[/green] digest: {p}")


@cli.command()
@click.option("--days", type=int, default=30, help="Analyze data from past how many days")
@click.option(
    "--min-score",
    type=float,
    default=50.0,
    help="Only view positions above this score (focus on your fit). Set 0 for entire market.",
)
@click.option(
    "--format",
    "formats",
    multiple=True,
    type=click.Choice(["md", "html"]),
    default=("md", "html"),
    help="Output format, multiple allowed",
)
@click.option("--email", is_flag=True, help="Also send via SMTP email")
def trends(days, min_score, formats, email):
    """Analyze job market trends: main players / tech stack heat / salary levels / recommendations."""
    config = _load_config()
    console.print(
        f"[cyan]→ Analyzing market trends for past {days} days "
        f"(score >= {min_score})...[/cyan]"
    )
    paths = generate_trends_report(
        config, days=days, min_score=min_score,
        formats=tuple(formats), send_email=email,
    )
    for fmt, p in paths.items():
        console.print(f"[green]✓[/green] {fmt}: {p}")


@cli.command()
@click.option("--limit", type=int, default=None, help="Max how many to fill this time (default all)")
def enrich(limit):
    """Call LLM to fill missing work_mode/min_education for old scored positions."""
    from .enricher import enrich_pending
    config = _load_config()
    done = enrich_pending(config, limit=limit)
    console.print(f"[green]✓[/green] Filled {done} positions")


@cli.command("backfill-hash")
@click.option("--dry-run", is_flag=True, help="Preview only, don't write to DB")
def backfill_hash(dry_run):
    """Fill content_hash for historical positions (cross-platform semantic deduplication field).

    Newly collected positions auto-include hash, this command for old data only.
    """
    from .dedup import content_hash as _hash, dedup_key
    from sqlalchemy import update as _update

    config = _load_config()
    db_path = config.path("db_path")
    updated = 0
    skipped = 0

    with session_scope(db_path) as session:
        jobs = session.scalars(
            select(Job).where(Job.content_hash.is_(None))
        ).all()
        console.print(f"Found {len(jobs)} positions without content_hash")

        for job in jobs:
            h = _hash(job.title, job.company, job.location or "")
            if dry_run:
                console.print(
                    f"  #{job.id} {job.title} @ {job.company} "
                    f"→ {h} ({dedup_key(job.title, job.company, job.location or '')})"
                )
                skipped += 1
            else:
                job.content_hash = h
                session.add(job)
                updated += 1

        if not dry_run:
            session.commit()

    if dry_run:
        console.print(f"[yellow]dry-run[/yellow] previewed {skipped} entries (not written)")
    else:
        console.print(f"[green]✓[/green] Filled content_hash for {updated} positions")


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", type=int, default=8765)
def web(host, port):
    """Start local web UI: view positions / click to trigger Claude resume edit + PDF."""
    from .web import run_server
    config = _load_config()
    run_server(config, host=host, port=port)


@cli.command("run-all")
@click.option(
    "--platform",
    type=click.Choice(PLATFORMS + ["all"]),
    default="all",
)
@click.option("--no-collect", is_flag=True, help="Skip collection step")
@click.option("--no-digest", is_flag=True, help="Skip daily digest email")
@click.option("--no-trends", is_flag=True, help="Skip trends report email")
def run_all(platform, no_collect, no_digest, no_trends):
    """Daily full workflow: LangGraph multi-agent orchestration. No automatic applies.

    Good for cron trigger. Apply to whichever you want by manually running 'apply --job-id N', default workflow doesn't apply.
    """
    config = _load_config()
    from .multiagent import JobAgentRunOptions, run_job_agent_graph

    # 拿当前画像 label
    from .profile_analyzer import get_current_profile_id
    from .db import Profile
    profile_id = get_current_profile_id(config)
    label = None
    if profile_id:
        with session_scope(config.path("db_path")) as session:
            row = session.get(Profile, profile_id)
            if row:
                label = row.label

    trigger = "cron" if os.environ.get("JOBHUNTER_TRIGGER") == "cron" else "cli"
    plats = PLATFORMS if platform == "all" else [platform]
    options = JobAgentRunOptions(
        platforms=plats,
        collect=not no_collect,
        digest=not no_digest,
        trends=not no_trends,
        trigger=trigger,
        profile_id=profile_id,
        profile_label=label,
    )

    console.print("[bold]→ Starting LangGraph multi-agent workflow...[/bold]")
    final_state = run_job_agent_graph(config, options)
    for ev in final_state.get("events", []):
        data = f" {ev.get('data')}" if ev.get("data") else ""
        console.print(f"[dim]{ev.get('agent')}:[/dim] {ev.get('message')}{data}")

    console.print("[green bold]✓ Full workflow complete (no applications triggered)[/green bold]")


@cli.command()
@click.option("--job-id", type=int, required=True)
def show(job_id):
    """Show details for a position."""
    config = _load_config()
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            console.print(f"[red]Job #{job_id} does not exist[/red]")
            sys.exit(1)

        console.print(f"[bold]#{job.id} {job.title}[/bold] @ {job.company}")
        console.print(f"  Status: {job.status} | Overall: {job.match_score}")
        console.print(f"  URL: {job.url}")
        console.print(f"  Location: {job.location} | Salary: {job.salary}")

        # 6 dimension sub-scores
        if job.score_skills is not None:
            tbl = Table(title="6 dimension sub-scores", show_header=False, box=None)
            tbl.add_column("Dimension", style="bold")
            tbl.add_column("Score", justify="right")
            tbl.add_column("Max", justify="right", style="dim")
            tbl.add_row("Background",       f"{job.score_background or 0:.0f}", "10")
            tbl.add_row("Skills overlap",   f"{job.score_skills or 0:.0f}", "30")
            tbl.add_row("Experience",       f"{job.score_experience or 0:.0f}", "30")
            tbl.add_row("Seniority",        f"{job.score_seniority or 0:.0f}", "10")
            tbl.add_row("Authorization",    f"{job.score_authorization or 0:.0f}", "10")
            tbl.add_row("Company type",     f"{job.score_company or 0:.0f}", "10")
            console.print(tbl)

        if job.match_summary:
            console.print(f"\n[bold]Summary:[/bold] {job.match_summary}")
        if job.match_connector:
            console.print(f"\n[bold cyan]Connector (cover letter hook):[/bold cyan] {job.match_connector}")
        if job.match_fit_bullets:
            console.print(f"\n[bold green]Fit bullets:[/bold green]\n{job.match_fit_bullets}")
        if job.match_keywords:
            console.print(f"\n[bold magenta]Keywords:[/bold magenta] {', '.join(job.match_keywords.splitlines())}")
        if job.match_gaps:
            console.print(f"\n[bold yellow]Gaps:[/bold yellow]\n{job.match_gaps}")
        if job.tailored_resume_path:
            console.print(f"\nCustomized resume: {job.tailored_resume_path}")
        if job.cover_letter_path:
            console.print(f"Cover letter: {job.cover_letter_path}")
        if job.description:
            console.print(f"\n[bold]JD:[/bold]\n{job.description[:1500]}...")


if __name__ == "__main__":
    cli()
