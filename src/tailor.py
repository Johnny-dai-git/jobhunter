"""简历定制器 — 两轮 pipeline.

Round 1 (DeepSeek, 便宜快):
    JD + 简历 + 资料库 → 差距分析 + 具体修改 plan

Round 2 (Claude Opus, 高质量):
    plan + 简历 + 资料库 → 最终定制简历

输出 2 个文件:
    {id:03d}_{company}_{title}_resume.md   (源,可编辑)
    {id:03d}_{company}_{title}_resume.pdf  (用于投递)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .agent import ClaudeClient, load_prompt, make_client, render
from .config import Config
from .db import Job, session_scope
from .pdf_generator import md_to_pdf
from .resume_reader import read_materials


# ── Round 1 Tool Schema (DeepSeek 生成 plan) ────────────────────────────────

RESUME_PLAN_TOOL: dict[str, Any] = {
    "name": "submit_resume_plan",
    "description": "提交简历修改 plan：差距分析 + 具体可执行的修改指令",
    "input_schema": {
        "type": "object",
        "properties": {
            "gap_analysis": {
                "type": "object",
                "description": "JD 与简历的差距分析",
                "properties": {
                    "jd_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "JD 中高频出现的技术词/动词/指标，这些是 ATS 核心",
                    },
                    "strong_matches": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "候选人简历中强匹配 JD 的经历/技能，直接保留并突出",
                    },
                    "gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "JD 要求但简历中明显不足或缺失的项（只列真实差距，不捏造）",
                    },
                    "hidden_strengths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "资料库中有但简历里没有充分展示的技术深度，可以补充",
                    },
                },
                "required": ["jd_keywords", "strong_matches", "gaps"],
            },
            "plan": {
                "type": "object",
                "description": "具体修改指令",
                "properties": {
                    "summary_rewrite": {
                        "type": "string",
                        "description": "Summary 段的改写方向：应该强调什么、用哪些 JD 关键词、第一句如何定位",
                    },
                    "skills_priority": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Skills 里按 JD 优先级排列的技能组，格式：'分组名: 技能1, 技能2'",
                    },
                    "experience_adjustments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "section": {"type": "string", "description": "哪个项目/经历"},
                                "action": {"type": "string", "enum": ["emphasize", "rewrite", "deprioritize", "add_from_materials"]},
                                "instruction": {"type": "string", "description": "具体怎么改，引用 JD 关键词"},
                            },
                            "required": ["section", "action", "instruction"],
                        },
                        "description": "每个经历/项目的具体调整指令",
                    },
                    "sections_order": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "最终简历的章节顺序，例如 ['Summary','Skills','Experience','Projects','Education','Publications']",
                    },
                    "keywords_to_inject": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "必须在简历中出现的 JD 关键词（只选候选人真实用过的）",
                    },
                },
                "required": ["summary_rewrite", "skills_priority", "experience_adjustments", "keywords_to_inject"],
            },
        },
        "required": ["gap_analysis", "plan"],
    },
}


# ── 核心函数 ─────────────────────────────────────────────────────────────────

def _extract_md_section(text: str) -> str:
    """从模型输出中抽出 ```markdown ... ``` 包裹的主体."""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _round1_plan(
    config: Config,
    resume_text: str,
    job: Job,
    extras_text: str,
) -> str:
    """Round 1: DeepSeek 分析 gap，生成修改 plan，返回 plan 的 JSON 字符串。"""
    client, model_name = make_client(config, "matcher")  # matcher → DeepSeek

    prompt = render(
        load_prompt("tailor_plan"),
        resume=resume_text,
        title=job.title,
        company=job.company,
        description=job.description or "(无 JD)",
        extras=extras_text or "(无附加材料)",
    )

    print(f"[tailor] Round 1 — DeepSeek 分析差距并生成 plan...")
    resp = client.messages.create(
        model=model_name,
        max_tokens=4096,
        tools=[RESUME_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_resume_plan"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_resume_plan":
            plan_data = block.input
            # 转成可读文本传给 Round 2
            return _plan_to_text(plan_data)

    raise RuntimeError("[tailor] Round 1 失败: DeepSeek 未返回 submit_resume_plan")


def _plan_to_text(plan_data: dict) -> str:
    """把结构化 plan 转成易读的文本，供 Claude 在 Round 2 执行。"""
    gap = plan_data.get("gap_analysis", {})
    plan = plan_data.get("plan", {})

    lines = ["## 差距分析\n"]

    if gap.get("jd_keywords"):
        lines.append("**JD 核心关键词**（必须在简历中出现）：")
        lines.append(", ".join(gap["jd_keywords"]))

    if gap.get("strong_matches"):
        lines.append("\n**强匹配项**（保留并突出）：")
        for item in gap["strong_matches"]:
            lines.append(f"- {item}")

    if gap.get("gaps"):
        lines.append("\n**差距项**（能包装的包装，不能的直接跳过）：")
        for item in gap["gaps"]:
            lines.append(f"- {item}")

    if gap.get("hidden_strengths"):
        lines.append("\n**资料库中的隐藏优势**（可补充到 bullet 中）：")
        for item in gap["hidden_strengths"]:
            lines.append(f"- {item}")

    lines.append("\n## 修改指令\n")

    if plan.get("summary_rewrite"):
        lines.append(f"**Summary 改写方向**：{plan['summary_rewrite']}")

    if plan.get("skills_priority"):
        lines.append("\n**Skills 优先级排列**：")
        for s in plan["skills_priority"]:
            lines.append(f"- {s}")

    if plan.get("experience_adjustments"):
        lines.append("\n**各经历/项目调整指令**：")
        for adj in plan["experience_adjustments"]:
            action_map = {
                "emphasize": "🔼 重点突出",
                "rewrite": "✏️ 改写",
                "deprioritize": "🔽 弱化",
                "add_from_materials": "📎 从资料库补充",
            }
            action_label = action_map.get(adj.get("action", ""), adj.get("action", ""))
            lines.append(f"- [{action_label}] **{adj.get('section', '')}**：{adj.get('instruction', '')}")

    if plan.get("keywords_to_inject"):
        lines.append(f"\n**必须注入的关键词**：{', '.join(plan['keywords_to_inject'])}")

    if plan.get("sections_order"):
        lines.append(f"\n**章节顺序**：{' → '.join(plan['sections_order'])}")

    return "\n".join(lines)


def tailor_for_job(
    config: Config,
    resume_text: str,
    job_id: int,
    candidate_name: str = "Candidate",
) -> Path:
    """两轮 pipeline 生成定制简历: .md + .pdf。返回 .md 路径。"""
    db_path = config.path("db_path")
    outputs_dir = config.path("outputs_dir")
    extras_text = read_materials(config.path("materials_dir"))

    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} 不存在")

        # ── Round 1: DeepSeek → plan ─────────────────────────────────────────
        try:
            plan_text = _round1_plan(config, resume_text, job, extras_text or "")
            print(f"[tailor] Round 1 完成，plan 长度: {len(plan_text)} 字符")
        except Exception as e:
            print(f"[tailor] Round 1 失败: {e}")
            raise RuntimeError(f"简历定制 Round 1 失败（DeepSeek 分析阶段）：{str(e)[:120]}") from e

        # ── Round 2: Claude Opus → 最终简历 ──────────────────────────────────
        print(f"[tailor] Round 2 — Claude Opus 执行 plan 改写简历...")
        client = ClaudeClient(config)
        prompt = render(
            load_prompt("tailor"),
            plan=plan_text,
            resume=resume_text,
            extras=extras_text or "(无附加材料)",
            candidate_name=candidate_name,
        )
        # 简历改写输出可能较长（完整 markdown + 各项目详细 bullet），给足 token 空间
        text = client.complete("tailor", prompt, max_tokens=8000)
        md_body = _extract_md_section(text)
        print(f"[tailor] Round 2 完成，简历长度: {len(md_body)} 字符")

        # ── 写文件 ────────────────────────────────────────────────────────────
        safe_company = re.sub(r"[^\w\-]+", "_", job.company)[:40]
        safe_title   = re.sub(r"[^\w\-]+", "_", job.title)[:40]
        base = f"{job.id:03d}_{safe_company}_{safe_title}_resume"

        md_path  = outputs_dir / f"{base}.md"
        pdf_path = outputs_dir / f"{base}.pdf"

        # md 文件保存完整输出（含改写说明），方便回顾
        full_output = f"<!-- Plan -->\n<!--\n{plan_text}\n-->\n\n{text}"
        md_path.write_text(full_output, encoding="utf-8")

        try:
            md_to_pdf(md_body, pdf_path)
        except Exception as e:
            print(f"[tailor] PDF 生成失败 ({e}), 只留 .md")
            pdf_path = None

        job.tailored_resume_path = str(md_path)
        if pdf_path:
            job.tailored_resume_pdf_path = str(pdf_path)
        session.add(job)
        session.commit()

    return md_path
