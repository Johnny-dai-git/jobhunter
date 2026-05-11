"""每日 digest: 把 Top-N 高匹配岗位生成 HTML 邮件并发送.

借鉴 n8n 工作流的最后两步:
- Build Email HTML
- Send a message (Gmail)
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import select

from .config import Config
from .db import Job, JobStatus, session_scope


def top_jobs(config: Config, top_n: int) -> list[Job]:
    """取最近评分的高分岗位."""
    db_path = config.path("db_path")
    cutoff = datetime.utcnow() - timedelta(days=2)
    with session_scope(db_path) as session:
        stmt = (
            select(Job)
            .where(
                Job.status.in_([JobStatus.SCORED.value, JobStatus.SHORTLISTED.value]),
                Job.match_score.is_not(None),
                Job.updated_at >= cutoff,
            )
            .order_by(Job.match_score.desc())
            .limit(top_n)
        )
        # detach so caller can use after session close
        jobs = list(session.scalars(stmt).all())
        for j in jobs:
            session.expunge(j)
        return jobs


def build_html(jobs: list[Job], min_recommend: float) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for j in jobs:
        score = j.match_score or 0
        color = "#16a34a" if score >= 80 else ("#ca8a04" if score >= 65 else "#6b7280")
        strengths = (j.match_strengths or "").replace("\n", "<br>")
        gaps = (j.match_gaps or "").replace("\n", "<br>")
        rows.append(f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #e5e7eb;vertical-align:top">
            <div style="font-size:24px;font-weight:700;color:{color}">{score:.0f}</div>
          </td>
          <td style="padding:12px;border-bottom:1px solid #e5e7eb">
            <div style="font-size:16px;font-weight:600">
              <a href="{j.url}" style="color:#1f2937;text-decoration:none">{_esc(j.title)}</a>
            </div>
            <div style="color:#6b7280;font-size:14px;margin-top:2px">
              {_esc(j.company)} &middot; {_esc(j.location or '')} &middot;
              <span style="text-transform:uppercase;font-size:11px">{j.source}</span>
            </div>
            <div style="margin-top:8px;color:#374151;font-size:14px">
              {_esc(j.match_summary or '')}
            </div>
            <div style="margin-top:8px;font-size:13px;color:#16a34a">
              <b>命中:</b><br>{strengths}
            </div>
            <div style="margin-top:6px;font-size:13px;color:#dc2626">
              <b>差距:</b><br>{gaps}
            </div>
          </td>
        </tr>
        """)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f9fafb;padding:20px">
  <div style="max-width:720px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
    <div style="background:#111827;color:white;padding:20px">
      <div style="font-size:20px;font-weight:600">每日 JobHunter 推送</div>
      <div style="color:#9ca3af;font-size:13px;margin-top:4px">{today} &middot; Top {len(jobs)} &middot; 推荐分阈值 {min_recommend:.0f}</div>
    </div>
    <table style="width:100%;border-collapse:collapse">
      {''.join(rows) if rows else '<tr><td style="padding:30px;text-align:center;color:#6b7280">今天没有新匹配的岗位 🎉</td></tr>'}
    </table>
    <div style="padding:16px;background:#f9fafb;color:#6b7280;font-size:12px;text-align:center">
      由 JobHunter 自动生成 · 想跳过某个岗位回复 archive #&lt;id&gt;
    </div>
  </div>
</body></html>"""
    return html


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_email(config: Config, html: str, subject: str | None = None) -> bool:
    """通过 SMTP 发邮件. 失败返回 False (不抛异常,因为 digest 失败不该阻断主流程)."""
    cfg = config.digest
    to = cfg.get("to")
    if not to:
        print("[digest] 未配置收件邮箱,跳过发送")
        return False
    smtp_cfg = cfg.get("smtp", {})
    host = smtp_cfg.get("host")
    port = int(smtp_cfg.get("port", 587))
    username = smtp_cfg.get("username")
    password_env = smtp_cfg.get("password_env", "SMTP_PASSWORD")
    password = config.env(password_env)
    if not (host and username and password):
        print(f"[digest] SMTP 未完整配置 (需要 username + {password_env}),跳过发送")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject or f"每日 JobHunter 推送 - {datetime.now():%Y-%m-%d}"
    msg["From"] = username
    msg["To"] = to
    msg.set_content("此邮件需要 HTML 模式查看")
    msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(username, password)
            s.send_message(msg)
        print(f"[digest] 已发送给 {to}")
        return True
    except Exception as e:
        print(f"[digest] 邮件发送失败: {e}")
        return False


def run_digest(config: Config) -> Path:
    """生成 HTML digest,可选发邮件,把 HTML 文件落到 outputs/."""
    cfg = config.digest
    top_n = int(cfg.get("top_n", 15))
    min_recommend = float(config.scoring.get("min_recommend_score", 70))

    jobs = top_jobs(config, top_n)
    html = build_html(jobs, min_recommend)

    outputs_dir = config.path("outputs_dir")
    out_path = outputs_dir / f"digest_{datetime.now():%Y%m%d}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[digest] HTML 已生成: {out_path}")

    if cfg.get("enabled", False):
        send_email(config, html)

    return out_path
