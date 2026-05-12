"""JobHunter CLI 入口."""
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
        console.print(f"[red]加载配置失败:[/red] {e}")
        sys.exit(1)


@click.group()
def cli():
    """JobHunter — 个人求职 agent."""


@cli.command()
def init():
    """初始化数据库和目录结构."""
    config = _load_config()
    db_path = config.path("db_path")
    init_db(db_path)
    config.path("resume_dir")
    config.path("jobs_dir")
    config.path("outputs_dir")
    console.print(f"[green]✓[/green] 数据库已建好: {db_path}")
    console.print(f"[green]✓[/green] 把简历放进: {config.path('resume_dir')}")


@cli.command("parse-resume")
def parse_resume():
    """读取简历并缓存为纯文本."""
    config = _load_config()
    resume_dir = config.path("resume_dir")
    src, text = parse_and_cache(resume_dir)
    console.print(f"[green]✓[/green] 已解析: {src.name} ({len(text)} 字符)")


@cli.command("analyze-profile")
@click.option("--force", is_flag=True, help="覆盖现有 _profile.json 重新分析")
def analyze_profile_cmd(force):
    """让 DeepSeek 读你的简历, 推断 Top-10 能力方向 + 真实搜索 title.

    第一步定 10 个 primary, 然后 collect 阶段用 aliases + broader_terms 模糊扩展.
    结果存到 data/resume/_profile.json.
    """
    config = _load_config()
    existing = load_profile(config)
    if existing and not force:
        console.print("[yellow]已有 profile 缓存, 加 --force 强制重新分析.[/yellow]")
        _print_profile(existing)
        return

    console.print("[cyan]→ DeepSeek 正在读你的简历...[/cyan]")
    profile = _analyze_profile(config)
    path = save_profile(config, profile)
    console.print(f"[green]✓[/green] 已保存到 {path}\n")
    _print_profile(profile)


def _print_profile(profile):
    """打印 profile 结果给用户看 (新版: 三维度评分 + aliases + broader_terms)."""
    console.print(f"[bold]核心定位:[/bold] {profile.summary}\n")

    tbl = Table(
        title="Top 10 最优投递岗位 (按 composite 降序)",
        show_lines=True,
    )
    tbl.add_column("#", style="bold")
    tbl.add_column("Primary + Aliases (搜索用)")
    tbl.add_column("Dir", style="dim")
    tbl.add_column("M/C/A→Comp", justify="center", style="bold cyan")
    tbl.add_column("为什么投这个", style="dim")
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
        "[dim]M = market_demand  C = competition (越低越好)  "
        "A = user_advantage  Comp = composite[/dim]"
    )

    # 汇总收集器用的搜索词 (Top-10 primary + aliases + broader_terms 模糊扩展)
    all_terms = profile.search_titles(
        include_aliases=True, include_broader=True, limit=40
    )
    console.print(
        f"\n[bold]Collect 实际会用 {len(all_terms)} 个搜索词 "
        f"(Top-10 primary + aliases + broader_terms):[/bold]"
    )
    for t in all_terms:
        console.print(f"  • {t}")

    console.print(
        f"\n[bold]目标地点:[/bold] " + ", ".join(profile.target_locations)
    )

    console.print("\n[bold]LinkedIn 直链 (primary title):[/bold]")
    for p in profile.top_10_positions:
        console.print(f"  • [link]{p.linkedin_search_url}[/link]")

    # 区域公司推荐
    if profile.recommended_companies:
        console.print(
            "\n[bold green]目标公司清单 (按区域,背景匹配 + 积极扩张):[/bold green]"
        )
        for region_label, companies in profile.recommended_companies.regions():
            if not companies:
                continue
            tbl = Table(title=f"🌍 {region_label} ({len(companies)} 家)", show_lines=True)
            tbl.add_column("公司", style="bold")
            tbl.add_column("为什么 fit", style="dim", overflow="fold")
            tbl.add_column("扩张/招聘信号", overflow="fold")
            tbl.add_column("例子 roles", style="cyan", overflow="fold")
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
    help="要登录的平台",
)
@click.option("--timeout", type=int, default=300, help="等待登录的最大秒数")
def login(platform, timeout):
    """打开浏览器让你手动登录某平台,然后保存 cookies."""
    config = _load_config()
    p = login_and_save(config, platform, timeout_sec=timeout)
    console.print(f"[green]✓[/green] {platform} cookies 保存到 {p}")


@cli.command()
@click.option(
    "--platform",
    type=click.Choice(PLATFORMS + ["all"]),
    default="all",
    help="要采集哪个平台",
)
def collect(platform):
    """从招聘平台抓岗位,自动去重落库."""
    config = _load_config()
    plats = PLATFORMS if platform == "all" else [platform]
    stats = collect_all(config, plats)
    console.print(
        f"\n[green]✓[/green] 共新增 {stats['total_new']} 个岗位 "
        f"(去重 {stats['total_seen']} 个)"
    )
    for plat, s in stats["by_platform"].items():
        console.print(f"  {plat}: 新增 {s['new']}, 重复 {s['duplicate']}")


@cli.command("add-job")
@click.option("--url", required=True)
@click.option("--title", required=True)
@click.option("--company", required=True)
@click.option("--location", default=None)
@click.option("--salary", default=None)
@click.option("--source", default="manual")
@click.option("--jd-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--jd", default=None)
def add_job(url, title, company, location, salary, source, jd_file, jd):
    """手动添加一个岗位."""
    config = _load_config()
    description = None
    if jd_file:
        description = Path(jd_file).read_text(encoding="utf-8")
    elif jd:
        description = jd

    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = Job(
            source=source,
            url=url,
            title=title,
            company=company,
            location=location,
            salary=salary,
            description=description,
            status=JobStatus.NEW.value,
        )
        session.add(job)
        session.commit()
        console.print(f"[green]✓[/green] 已添加 #{job.id}: {title} @ {company}")


@cli.command()
@click.option("--limit", type=int, default=None)
def match(limit):
    """对所有未评分的岗位调用 Claude 打分."""
    config = _load_config()
    resume_dir = config.path("resume_dir")
    try:
        resume_text = load_cached(resume_dir)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        console.print("先把简历放进 data/resume/,然后跑 `parse-resume`")
        sys.exit(1)

    console.print("[cyan]正在评分...[/cyan]")
    results = score_pending(config, resume_text, limit=limit)
    if not results:
        console.print("[yellow]没有未评分的岗位.[/yellow]")
        return

    table = Table(title=f"评分结果 ({len(results)} 个)")
    table.add_column("ID", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("摘要")
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
    """查看追踪表."""
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
        console.print("[yellow]没有匹配的岗位.[/yellow]")
        return

    table = Table(title=f"岗位列表 ({len(jobs)})")
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
    """给某岗位生成定制简历和求职信."""
    config = _load_config()
    resume_text = load_cached(config.path("resume_dir"))

    console.print("[cyan]→ 生成定制简历...[/cyan]")
    resume_path = tailor_for_job(config, resume_text, job_id, candidate_name=name)
    console.print(f"[green]✓[/green] 简历: {resume_path}")

    if not no_cover:
        console.print("[cyan]→ 生成求职信...[/cyan]")
        cover_path = write_cover_letter(config, resume_text, job_id)
        console.print(f"[green]✓[/green] 求职信: {cover_path}")


@cli.command("mark-applied")
@click.option("--job-id", type=int, required=True)
@click.option("--note", default=None, help="可选备注,比如 '通过公司官网投递'")
def mark_applied(job_id, note):
    """你手动投完后告诉 agent: 这岗位已投. 仅做状态记录,不触发任何动作."""
    config = _load_config()
    _mark_applied(config, job_id, note)
    console.print(f"[green]✓[/green] #{job_id} 已标记为 applied")


@cli.command()
def digest():
    """生成每日 Top-N digest HTML,可选发邮件."""
    config = _load_config()
    p = run_digest(config)
    console.print(f"[green]✓[/green] digest: {p}")


@cli.command()
@click.option("--days", type=int, default=30, help="分析过去多少天的数据")
@click.option(
    "--min-score",
    type=float,
    default=50.0,
    help="只看高于此分数的岗位 (聚焦适合你的). 设 0 看全部市场.",
)
@click.option(
    "--format",
    "formats",
    multiple=True,
    type=click.Choice(["md", "html"]),
    default=("md", "html"),
    help="输出格式,可指定多个",
)
@click.option("--email", is_flag=True, help="同时通过 SMTP 邮件发出")
def trends(days, min_score, formats, email):
    """分析求职市场趋势: 主要 player / 技术栈热度 / 薪资水位 / 给你的建议."""
    config = _load_config()
    console.print(
        f"[cyan]→ 分析过去 {days} 天 "
        f"(score >= {min_score}) 的市场趋势...[/cyan]"
    )
    paths = generate_trends_report(
        config, days=days, min_score=min_score,
        formats=tuple(formats), send_email=email,
    )
    for fmt, p in paths.items():
        console.print(f"[green]✓[/green] {fmt}: {p}")


@cli.command()
@click.option("--limit", type=int, default=None, help="本次最多补填多少个 (默认全部)")
def enrich(limit):
    """对已评分但缺 work_mode/min_education 的旧岗位调 LLM 补填这两个字段."""
    from .enricher import enrich_pending
    config = _load_config()
    done = enrich_pending(config, limit=limit)
    console.print(f"[green]✓[/green] 补填了 {done} 个岗位")


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", type=int, default=8765)
def web(host, port):
    """启动本地 web UI: 查看岗位 / 点击触发 Claude 改简历 + PDF."""
    from .web import run_server
    config = _load_config()
    run_server(config, host=host, port=port)


@cli.command("run-all")
@click.option(
    "--platform",
    type=click.Choice(PLATFORMS + ["all"]),
    default="all",
)
@click.option("--no-collect", is_flag=True, help="跳过采集步骤")
@click.option("--no-digest", is_flag=True, help="跳过发 daily digest 邮件")
@click.option("--no-trends", is_flag=True, help="跳过趋势报告邮件")
def run_all(platform, no_collect, no_digest, no_trends):
    """每日全流程: LangGraph multi-agent orchestration. 不含 apply.

    适合 cron 触发. 你想投哪个就手动跑 'apply --job-id N',默认流程不投.
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

    console.print("[bold]→ 启动 LangGraph multi-agent workflow...[/bold]")
    final_state = run_job_agent_graph(config, options)
    for ev in final_state.get("events", []):
        data = f" {ev.get('data')}" if ev.get("data") else ""
        console.print(f"[dim]{ev.get('agent')}:[/dim] {ev.get('message')}{data}")

    console.print("[green bold]✓ 全流程完成 (未触发任何投递)[/green bold]")


@cli.command()
@click.option("--job-id", type=int, required=True)
def show(job_id):
    """显示某岗位的详细信息."""
    config = _load_config()
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            console.print(f"[red]Job #{job_id} 不存在[/red]")
            sys.exit(1)

        console.print(f"[bold]#{job.id} {job.title}[/bold] @ {job.company}")
        console.print(f"  Status: {job.status} | Overall: {job.match_score}")
        console.print(f"  URL: {job.url}")
        console.print(f"  Location: {job.location} | Salary: {job.salary}")

        # 6 维度子分
        if job.score_skills is not None:
            tbl = Table(title="6 维度子分", show_header=False, box=None)
            tbl.add_column("维度", style="bold")
            tbl.add_column("分数", justify="right")
            tbl.add_column("满分", justify="right", style="dim")
            tbl.add_row("Background",       f"{job.score_background or 0:.0f}", "10")
            tbl.add_row("Skills overlap",   f"{job.score_skills or 0:.0f}", "30")
            tbl.add_row("Experience",       f"{job.score_experience or 0:.0f}", "30")
            tbl.add_row("Seniority",        f"{job.score_seniority or 0:.0f}", "10")
            tbl.add_row("Authorization",    f"{job.score_authorization or 0:.0f}", "10")
            tbl.add_row("Company type",     f"{job.score_company or 0:.0f}", "10")
            console.print(tbl)

        if job.match_summary:
            console.print(f"\n[bold]摘要:[/bold] {job.match_summary}")
        if job.match_connector:
            console.print(f"\n[bold cyan]Connector (求职信钩子):[/bold cyan] {job.match_connector}")
        if job.match_fit_bullets:
            console.print(f"\n[bold green]Fit bullets:[/bold green]\n{job.match_fit_bullets}")
        if job.match_keywords:
            console.print(f"\n[bold magenta]Keywords:[/bold magenta] {', '.join(job.match_keywords.splitlines())}")
        if job.match_gaps:
            console.print(f"\n[bold yellow]Gaps:[/bold yellow]\n{job.match_gaps}")
        if job.tailored_resume_path:
            console.print(f"\n定制简历: {job.tailored_resume_path}")
        if job.cover_letter_path:
            console.print(f"求职信: {job.cover_letter_path}")
        if job.description:
            console.print(f"\n[bold]JD:[/bold]\n{job.description[:1500]}...")


if __name__ == "__main__":
    cli()
