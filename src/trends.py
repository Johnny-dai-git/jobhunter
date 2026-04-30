"""求职市场趋势分析.

工作流:
1. 从 DB 拉过去 N 天采集到的岗位 (按 min_score 过滤,只看适合候选人的)
2. Python 端做聚合: 公司频次、技能频次、地点、薪资、资历分布
3. 把聚合数据 + 几条样本 JD + 候选人简历喂给 Claude,生成 narrative
4. 输出 markdown / HTML 报告

设计原则:
- Python 做"算数",Claude 做"诠释". 不让 Claude 数数,数据准确性 100%.
- 报告里 narrative 部分由 Claude 写,明细表格由 Python 渲染.
- Claude 拿到的 stats 是预聚合好的,不是原始 JDs,既省 token 又能控制 hallucination.
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


# ============ 关键词词典 ============
# 不追求全,追求"在科技岗位 JD 里高频出现且能识别趋势的"
TECH_KEYWORDS = [
    # 语言
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Golang", "Rust",
    "C++", "C#", "Ruby", "Swift", "Kotlin", "Scala",
    # 前端
    "React", "Vue", "Angular", "Next.js", "Svelte", "Tailwind",
    # 后端框架
    "Django", "Flask", "FastAPI", "Spring", "Express", "Rails", "NestJS",
    # 云 / Infra
    "AWS", "GCP", "Azure", "Kubernetes", "Docker", "Terraform", "Ansible",
    "Linux", "CI/CD", "DevOps", "SRE",
    # 数据
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "Snowflake",
    "BigQuery", "Spark", "Kafka", "Airflow", "dbt",
    # AI / ML
    "TensorFlow", "PyTorch", "JAX", "LangChain", "LlamaIndex", "OpenAI",
    "Claude", "Anthropic", "Gemini", "LLM", "RAG", "Vector Database",
    "Hugging Face", "Transformers", "Fine-tuning", "Embeddings",
    "Machine Learning", "Deep Learning", "Computer Vision", "NLP",
    "Reinforcement Learning", "MLOps",
    # 协议 / 架构
    "GraphQL", "REST", "gRPC", "WebSocket", "Microservices", "Event-driven",
    "Distributed Systems",
    # 其他
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


# ============ 提取器 ============
def extract_skills(text: str, vocab: list[str] = TECH_KEYWORDS) -> set[str]:
    """从 JD 文本里抽取已知技能. 用 word-boundary 减少误匹配."""
    if not text:
        return set()
    found: set[str] = set()
    lower = text.lower()
    for kw in vocab:
        kl = kw.lower()
        # 多词的直接 substring;单词带词边界
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
    """解析 '$120k - $150k' / '$120,000-$150,000' / '$80K to $100K USD' 等."""
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
                # "120k" 可能写成纯 "120" — 视范围放大
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
        # 抓城市
        city = re.split(r"[·,•]", s)[0].strip()
        return f"Hybrid - {city}" if city and "hybrid" not in city.lower() else "Hybrid"
    # 取第一段做城市
    return re.split(r"[·,•]", s)[0].strip().title() or "unspecified"


# ============ 聚合 ============
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

    # 公司
    companies = Counter(j.company for j in jobs if j.company)
    stats["top_companies"] = companies.most_common(15)

    # 技能 (从 title + description 提取)
    skill_counts: Counter[str] = Counter()
    for j in jobs:
        text = f"{j.title or ''}\n{j.description or ''}"
        for s in extract_skills(text):
            skill_counts[s] += 1
    stats["top_skills"] = skill_counts.most_common(30)

    # 地点
    loc_counts: Counter[str] = Counter()
    for j in jobs:
        loc_counts[normalize_location(j.location)] += 1
    stats["top_locations"] = loc_counts.most_common(15)

    # 资历
    sen_counts: Counter[str] = Counter()
    for j in jobs:
        sen_counts[extract_seniority(j.title or "", j.description or "")] += 1
    stats["seniority"] = dict(sen_counts.most_common())

    # 薪资 (有数据才统计)
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

    # 评分分布
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

    # 来源
    stats["by_source"] = dict(Counter(j.source for j in jobs).most_common())

    # Top jobs 给 narrative 用 + 报告底部表格
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

    # 6 条样本 JD 给 Claude 做语境
    samples = []
    for j in top_jobs_objs[:6]:
        samples.append({
            "title": j.title,
            "company": j.company,
            "score": float(j.match_score or 0),
            "description": (j.description or "")[:1200],
        })
    stats["_sample_jds"] = samples  # 下划线表示不进最终报告表格

    return stats


# ============ Claude narrative ============
def analyze_with_claude(
    config: Config,
    stats: dict[str, Any],
    resume_text: str,
) -> str:
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
    # trends 默认走 DeepSeek V4-Pro (1M context, 便宜)
    client, model_name = make_client(config, "trends")
    resp = client.messages.create(
        model=model_name,
        max_tokens=3500,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip()


# ============ 报告渲染 ============
def render_markdown(stats: dict[str, Any], narrative: str) -> str:
    L: list[str] = []
    L.append(f"# 求职市场趋势报告")
    L.append("")
    L.append(
        f"> {stats['generated_at']} · 过去 {stats['period_days']} 天 · "
        f"分析 {stats['total_jobs']} 个岗位"
        + (f" · 仅看 score >= {stats['min_score_filter']:.0f}" if stats.get("min_score_filter") else "")
    )
    L.append("")
    if stats["total_jobs"] == 0:
        L.append("⚠️ 这段时间没有采集到符合条件的岗位. 先跑 `collect` 和 `match`.")
        return "\n".join(L)

    L.append(narrative)
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 数据明细")
    L.append("")

    # Top 公司
    L.append("### Top 雇主")
    L.append("")
    L.append("| 公司 | 岗位数 |")
    L.append("|---|---:|")
    for c, n in stats.get("top_companies", []):
        L.append(f"| {c} | {n} |")
    L.append("")

    # 技能
    L.append("### 技术栈热度 Top 30")
    L.append("")
    L.append("| 技能 | 出现次数 | 占比 |")
    L.append("|---|---:|---:|")
    total = stats["total_jobs"]
    for s, n in stats.get("top_skills", []):
        pct = 100 * n / total if total else 0
        L.append(f"| {s} | {n} | {pct:.0f}% |")
    L.append("")

    # 地点
    L.append("### 地点分布")
    L.append("")
    L.append("| 地点 | 岗位数 |")
    L.append("|---|---:|")
    for loc, n in stats.get("top_locations", []):
        L.append(f"| {loc} | {n} |")
    L.append("")

    # 薪资
    if stats.get("salary"):
        s = stats["salary"]
        L.append(f"### 薪资水位 (样本数: {s['samples']})")
        L.append("")
        L.append(f"- 起薪中位数: **${s['median_low']:,}**")
        L.append(f"- 上限中位数: **${s['median_high']:,}**")
        L.append(f"- P25 起薪 / P75 上限: ${s['p25_low']:,} / ${s['p75_high']:,}")
        L.append(f"- 整体范围: ${s['min']:,} - ${s['max']:,}")
        L.append("")

    # 资历
    L.append("### 资历分布")
    L.append("")
    for level, count in stats.get("seniority", {}).items():
        L.append(f"- {level}: {count}")
    L.append("")

    # 来源
    L.append("### 来源分布")
    L.append("")
    for src, n in stats.get("by_source", {}).items():
        L.append(f"- {src}: {n}")
    L.append("")

    # 评分
    L.append("### 评分分布")
    L.append("")
    for b, n in stats.get("score_distribution", {}).items():
        L.append(f"- {b}: {n}")
    L.append("")

    # Top jobs
    L.append("## Top 10 高匹配岗位")
    L.append("")
    L.append("| ID | Score | Title | Company | Location |")
    L.append("|---:|---:|---|---|---|")
    for j in stats.get("top_jobs", []):
        L.append(
            f"| {j['id']} | {j['score']:.0f} | "
            f"[{j['title']}]({j['url']}) | {j['company']} | {j.get('location') or ''} |"
        )

    return "\n".join(L)


def render_html(md_text: str, title: str) -> str:
    """Markdown -> HTML (简单包装,不引外部样式表)."""
    try:
        import markdown as md_lib
        body = md_lib.markdown(md_text, extensions=["tables", "fenced_code"])
    except Exception:
        # fallback: 套 <pre>
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


# ============ 主入口 ============
def generate_report(
    config: Config,
    days: int = 30,
    min_score: float | None = None,
    formats: tuple[str, ...] = ("md",),
) -> dict[str, Path]:
    """生成趋势报告. 返回 {format: path}."""
    stats = aggregate_stats(config, days=days, min_score=min_score)

    if stats["total_jobs"] == 0:
        narrative = "⚠️ 没有采集到岗位数据. 先跑 `collect` + `match`,再来生成趋势报告."
    else:
        try:
            resume_text = load_cached(config.path("resume_dir"))
        except FileNotFoundError:
            resume_text = "(简历未配置 — 给出的建议会偏通用化)"
        narrative = analyze_with_claude(config, stats, resume_text)

    md_text = render_markdown(stats, narrative)
    outputs = config.path("outputs_dir")
    stamp = datetime.now().strftime("%Y%m%d")
    paths: dict[str, Path] = {}

    if "md" in formats:
        p = outputs / f"trends_{stamp}.md"
        p.write_text(md_text, encoding="utf-8")
        paths["md"] = p
    if "html" in formats:
        title = f"求职市场趋势 {stamp}"
        p = outputs / f"trends_{stamp}.html"
        p.write_text(render_html(md_text, title), encoding="utf-8")
        paths["html"] = p

    return paths
