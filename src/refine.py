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


def _migrate_message(msg: dict) -> dict:
    """兼容旧格式: 旧版 user message 里嵌入了完整简历，新版只存用户原始请求。
    检测方式: user content 里有 '---\n当前简历' 分隔符 → 截取分隔符前的部分。
    """
    if msg.get("role") == "user":
        content = msg.get("content", "")
        sep = "---\n当前简历"
        if sep in content:
            return {"role": "user", "content": content.split(sep)[0].strip()}
    return msg


def load_chat_history(config: Config, job_id: int) -> list[dict]:
    path = _chat_path(config, job_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 兼容旧格式：剥离 user message 中嵌入的简历全文
        migrated = [_migrate_message(m) for m in raw]
        # 如果有变化，原地回写，避免下次再做迁移
        if migrated != raw:
            path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
        return migrated
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
        session.refresh(rev)   # commit 后 expire，refresh 重新加载 id/version_num 等字段
        session.expunge(rev)   # 再 detach，外面访问属性才安全
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

    # 加载对话历史（只存储用户原始请求 + assistant 改动说明，不存嵌入简历的完整消息）
    history = load_chat_history(config, job_id)   # [{role, content}] 精简版本

    # 追加本轮用户请求（仅存原文，不含简历）
    history.append({"role": "user", "content": user_message})

    # 构造发给 API 的完整 messages：历史消息 + 最后一条注入当前简历
    # 只有最后一条 user message 需要带完整简历，历史消息只保留对话摘要
    api_messages: list[dict] = []
    for i, msg in enumerate(history):
        if msg["role"] == "user":
            is_last = (i == len(history) - 1)
            if is_last:
                # 最后一条注入最新简历
                api_messages.append({"role": "user", "content": REFINE_USER_TEMPLATE.format(
                    user_message=msg["content"],
                    current_resume=current_resume,
                )})
            else:
                # 历史用户消息只保留原始请求（简短），避免 context 爆炸
                api_messages.append({"role": "user", "content": msg["content"]})
        else:
            api_messages.append(msg)

    # 调用 Claude Opus
    client, model_name = make_client(config, "tailor")
    system_prompt = REFINE_SYSTEM_PROMPT.format(title=title, company=company)

    resp = client.messages.create(
        model=model_name,
        max_tokens=4096,
        system=system_prompt,
        messages=api_messages,
    )
    assistant_text = resp.content[0].text

    # 解析输出
    md_content, note = _extract_md_and_note(assistant_text)

    # 只存 assistant 的改动说明（不存完整简历），保持 history 精简
    history.append({"role": "assistant", "content": note or "(简历已更新)"})
    save_chat_history(config, job_id, history)

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

    # history 本身已经是精简版（user=原始请求，assistant=改动说明），直接返回
    return {
        "md_content": md_content,
        "note": note,
        "version_num": version_num,
        "messages": history,  # 已经是干净的精简格式
    }
