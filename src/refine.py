"""简历对话式精修模块.

流程:
  1. 用户发送修改请求 (自然语言)
  2. 系统自动注入 HR/HM 专业 prompt + 当前简历 + 对话历史
  3. Claude Opus 返回更新后的完整简历 + 改动说明
  4. 新版本存入 ResumeRevision 表
  5. 对话历史持久化到 JSON 文件，下次继续

每条用户消息都会自动 append 以下系统上下文:
  - HR 视角规则
  - HM 视角规则
  - 格式强制要求
  - 当前简历全文
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from .agent import make_client
from .config import Config
from .db import Job, ResumeRevision, session_scope
from .pdf_generator import md_to_pdf


# ── 系统 Prompt (每轮对话都注入) ─────────────────────────────────────────────

REFINE_SYSTEM_PROMPT = """\
你是一位顶级简历修改顾问，同时具备两种专业视角：

**HR 初筛视角（6 秒扫读）**
- ATS 关键词覆盖率：目标岗位的核心技术词是否出现在简历里
- 格式清晰度：标题层级、bullet 长短、空白节奏
- 第一屏冲击力：Summary + Skills 是否在最显眼位置
- 量化结果密度：数字和指标的密度够不够

**HM 技术深度视角**
- 每个项目的技术可信度：描述是否体现真实的系统设计能力
- 独立交付证明：有没有"我设计/我构建/我独立"的证据
- 差异化价值：这个候选人有什么是大多数人没有的
- 成果与难度匹配：数字背后的技术难度是否体现出来

---

**每次修改必须严格遵守以下规则：**

1. **不捏造任何内容** — 只能基于已有经历改写、重排序、换表述
2. **保持原有格式** — 严格保留原简历的章节结构、标题层级、排版风格和 bullet 格式。只改措辞和内容重点，不重新设计版面，不新增原来没有的章节
3. **保留所有超链接** — GitHub、Portfolio、LinkedIn、论文链接等，原样保留，不得丢失
4. **关键词加粗** — 每条 bullet 中最核心的技术词或量化成果用 `**词语**` 标注，每条不超过 2 处
5. **不出现元信息** — 简历中不能出现"Tailored for"、"针对岗位"、"修改版本"等字样
6. **输出完整简历** — 用 ```markdown 围起来，输出的是可以直接发给 HR 的完整简历，不是片段
7. **改动说明** — 在 ```markdown 围栏**外**附一段简短说明（≤100字）：改了什么、为什么这样改对 HR/HM 评分有帮助

---

当前目标岗位：**{title} @ {company}**
"""

REFINE_USER_TEMPLATE = """\
{user_message}

---
当前简历（请在此基础上修改）：

```markdown
{current_resume}
```
"""


# ── 对话历史管理 ──────────────────────────────────────────────────────────────

def _chat_path(config: Config, job_id: int) -> Path:
    outputs_dir = config.path("outputs_dir")
    return outputs_dir / f"{job_id:03d}_chat.json"


def load_chat_history(config: Config, job_id: int) -> list[dict]:
    path = _chat_path(config, job_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_chat_history(config: Config, job_id: int, messages: list[dict]) -> None:
    path = _chat_path(config, job_id)
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_chat_history(config: Config, job_id: int) -> None:
    path = _chat_path(config, job_id)
    if path.exists():
        path.unlink()


# ── 版本管理 ─────────────────────────────────────────────────────────────────

def get_revisions(config: Config, job_id: int) -> list[ResumeRevision]:
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        rows = list(session.scalars(
            select(ResumeRevision)
            .where(ResumeRevision.job_id == job_id)
            .order_by(ResumeRevision.version_num.desc())
        ).all())
        for r in rows:
            session.expunge(r)
        return rows


def get_revision(config: Config, job_id: int, version_num: int) -> Optional[ResumeRevision]:
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        row = session.scalars(
            select(ResumeRevision)
            .where(ResumeRevision.job_id == job_id, ResumeRevision.version_num == version_num)
        ).first()
        if row:
            session.expunge(row)
        return row


def save_revision(config: Config, job_id: int, md_content: str, note: str = "") -> ResumeRevision:
    """保存一个新版本，版本号自动递增。"""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        last = session.scalars(
            select(ResumeRevision)
            .where(ResumeRevision.job_id == job_id)
            .order_by(ResumeRevision.version_num.desc())
        ).first()
        next_num = (last.version_num + 1) if last else 1

        rev = ResumeRevision(
            job_id=job_id,
            version_num=next_num,
            md_content=md_content,
            note=note or f"Version {next_num}",
        )
        session.add(rev)
        session.commit()
        session.expunge(rev)
        return rev


def get_current_resume_md(config: Config, job_id: int) -> Optional[str]:
    """获取最新版本的简历 markdown，没有版本则读取原始 tailor 输出。"""
    revisions = get_revisions(config, job_id)
    if revisions:
        return revisions[0].md_content  # 按 version_num desc，第一个是最新版

    # fallback: 读取原始 tailor 输出的 md 文件
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if job and job.tailored_resume_path:
            path = Path(job.tailored_resume_path)
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                # 提取 ```markdown ... ``` 主体
                m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", raw, re.DOTALL)
                return m.group(1).strip() if m else raw.strip()
    return None


# ── 核心对话函数 ─────────────────────────────────────────────────────────────

def _extract_md_and_note(text: str) -> tuple[str, str]:
    """从 Claude 输出中分离出 markdown 主体和改动说明。"""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        md_body = m.group(1).strip()
        # 围栏外的文字是改动说明
        note = text[m.end():].strip()
        if not note:
            note = text[:m.start()].strip()
        return md_body, note[:300]
    return text.strip(), ""


def chat_refine(
    config: Config,
    job_id: int,
    user_message: str,
    auto_save: bool = True,
) -> dict:
    """发送一条对话消息，Claude 返回更新后的简历。

    返回:
      {
        "md_content": str,      # 新版简历 markdown
        "note": str,            # Claude 的改动说明
        "version_num": int,     # 保存的版本号 (auto_save=True 时)
        "messages": list[dict], # 完整对话历史 (供前端更新)
      }
    """
    db_path = config.path("db_path")

    # 获取岗位信息
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} 不存在")
        title, company = job.title, job.company

    # 获取当前简历
    current_resume = get_current_resume_md(config, job_id)
    if not current_resume:
        raise ValueError("还没有生成定制简历，请先点击「生成定制简历」")

    # 加载对话历史
    messages = load_chat_history(config, job_id)

    # 构造这一轮的 user message（注入当前简历）
    user_content = REFINE_USER_TEMPLATE.format(
        user_message=user_message,
        current_resume=current_resume,
    )
    messages.append({"role": "user", "content": user_content})

    # 调用 Claude Opus
    client, model_name = make_client(config, "tailor")
    system_prompt = REFINE_SYSTEM_PROMPT.format(title=title, company=company)

    resp = client.messages.create(
        model=model_name,
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
    )
    assistant_text = resp.content[0].text

    # 解析输出
    md_content, note = _extract_md_and_note(assistant_text)

    # 追加 assistant 回复到历史（存原始回复，不截断）
    messages.append({"role": "assistant", "content": assistant_text})
    save_chat_history(config, job_id, messages)

    # 保存版本
    version_num = None
    if auto_save and md_content:
        rev = save_revision(config, job_id, md_content, note=note)
        version_num = rev.version_num

        # 同时更新 PDF
        try:
            outputs_dir = config.path("outputs_dir")
            with session_scope(db_path) as session:
                job = session.get(Job, job_id)
                if job:
                    safe_company = re.sub(r"[^\w\-]+", "_", company)[:40]
                    safe_title   = re.sub(r"[^\w\-]+", "_", title)[:40]
                    pdf_path = outputs_dir / f"{job_id:03d}_{safe_company}_{safe_title}_v{version_num}.pdf"
                    md_to_pdf(md_content, pdf_path)
                    # 更新 job 的最新 pdf 路径
                    job.tailored_resume_pdf_path = str(pdf_path)
                    session.add(job)
                    session.commit()
        except Exception as e:
            print(f"[refine] PDF 生成失败: {e}")

    # 返回给前端的消息历史只保留 role/content 的摘要（不含嵌入的简历全文，减少传输）
    display_messages = []
    for msg in messages:
        if msg["role"] == "user":
            # 只展示用户原始请求，不展示嵌入的简历
            original_request = msg["content"].split("---\n当前简历")[0].strip()
            display_messages.append({"role": "user", "content": original_request})
        else:
            # assistant 只展示围栏外的说明部分
            _, note_text = _extract_md_and_note(msg["content"])
            display_messages.append({
                "role": "assistant",
                "content": note_text or "(简历已更新)",
            })

    return {
        "md_content": md_content,
        "note": note,
        "version_num": version_num,
        "messages": display_messages,
    }
