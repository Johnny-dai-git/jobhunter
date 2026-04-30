"""半自动投递助手.

设计:
- Claude (anthropic SDK) 担任 agent,通过 tool_use 调浏览器
- Playwright 跑一个可见的 Chromium,实际操作页面
- 默认在点提交按钮前暂停等用户确认 (auto_submit=False)
- 候选人信息、简历、cover letter 通过 system prompt 注入

技术参考: 借鉴 n8n 工作流的"工具节点"思路,但把 LLM 换成 Claude tool_use,把浏览器
节点换成 Playwright,这样选择器能动态适配各种 ATS (Greenhouse/Lever/Workday/Taleo).

⚠️ 投递是不可逆操作. 默认 auto_submit=False, agent 准备好提交时会暂停让你确认.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import load_prompt, make_client, render
from .config import Config
from .db import Event, Job, JobStatus, session_scope


# ============ Tool 定义 ============
APPLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_page",
        "description": "读取当前页面的可见文本和所有 form 字段(input/select/textarea/button)的列表",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fill_field",
        "description": "给一个 input/textarea 填值",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector 或 'label:文字' 形式"},
                "value": {"type": "string"},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "select_option",
        "description": "给下拉框选值",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "value": {"type": "string", "description": "选项的 value 或 label 文本"},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "click",
        "description": "点击按钮、复选框、链接等",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "upload_file",
        "description": "给 file input 上传一个文件 (绝对路径)",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["selector", "path"],
        },
    },
    {
        "name": "screenshot",
        "description": "对当前页面截图,返回简短描述+保存路径",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ready_to_submit",
        "description": "全部填好,准备提交. 默认会暂停让用户确认(auto_submit=false 时).",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "已填字段的简短汇总"},
                "submit_selector": {"type": "string", "description": "提交按钮的 selector"},
            },
            "required": ["summary", "submit_selector"],
        },
    },
    {
        "name": "give_up",
        "description": "无法继续时调用,解释原因",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


class ApplySession:
    """一次投递会话: 包装浏览器 + tool 调度."""

    def __init__(self, config: Config, job: Job, auto_submit: bool = False):
        self.config = config
        self.job = job
        self.auto_submit = auto_submit
        self._screenshots_dir = config.path("outputs_dir") / "screenshots"
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._page = None
        self._submit_done = False
        self._given_up_reason: str | None = None

    # --- 工具实装 ---
    def tool_read_page(self) -> str:
        page = self._page
        text = page.evaluate("() => document.body.innerText")[:3500]
        # 列出 form 字段
        fields = page.evaluate("""
        () => {
          const out = [];
          document.querySelectorAll("input, textarea, select, button").forEach(el => {
            const label = (el.labels && el.labels[0] && el.labels[0].innerText)
              || el.getAttribute("aria-label")
              || el.getAttribute("placeholder")
              || el.getAttribute("name")
              || "";
            out.push({
              tag: el.tagName.toLowerCase(),
              type: el.type || null,
              name: el.getAttribute("name"),
              id: el.id,
              label: label.trim().slice(0, 80),
              required: el.required || el.getAttribute("aria-required") === "true",
              visible: !!(el.offsetWidth || el.offsetHeight),
            });
          });
          return out.slice(0, 80);
        }
        """)
        return json.dumps(
            {"url": page.url, "text": text, "fields": fields},
            ensure_ascii=False,
        )

    def tool_fill(self, selector: str, value: str) -> str:
        loc = self._resolve_locator(selector)
        loc.fill(value)
        return f"已填 {selector}"

    def tool_select(self, selector: str, value: str) -> str:
        loc = self._resolve_locator(selector)
        try:
            loc.select_option(value=value)
        except Exception:
            loc.select_option(label=value)
        return f"已选 {selector} -> {value}"

    def tool_click(self, selector: str) -> str:
        loc = self._resolve_locator(selector)
        loc.click()
        self._page.wait_for_load_state("domcontentloaded", timeout=10000)
        return f"已点击 {selector}"

    def tool_upload(self, selector: str, path: str) -> str:
        loc = self._resolve_locator(selector)
        loc.set_input_files(path)
        return f"已上传 {path}"

    def tool_screenshot(self) -> str:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        p = self._screenshots_dir / f"job{self.job.id}_{ts}.png"
        self._page.screenshot(path=str(p), full_page=True)
        return f"截图已保存: {p}"

    def tool_ready_to_submit(self, summary: str, submit_selector: str) -> str:
        print("\n" + "=" * 60)
        print(f"准备提交 — Job #{self.job.id} {self.job.title} @ {self.job.company}")
        print("已填字段汇总:")
        print(summary)
        print("=" * 60)

        if self.auto_submit:
            try:
                self._resolve_locator(submit_selector).click()
                self._submit_done = True
                return "已自动提交"
            except Exception as e:
                return f"自动提交失败: {e}"

        # 默认: 暂停让用户确认
        ans = input("\n是否点击提交? [y/N]: ").strip().lower()
        if ans == "y":
            try:
                self._resolve_locator(submit_selector).click()
                self._submit_done = True
                return "用户确认后已提交"
            except Exception as e:
                return f"提交失败: {e}"
        else:
            return "用户取消了提交,任务中止"

    def tool_give_up(self, reason: str) -> str:
        self._given_up_reason = reason
        return f"已放弃: {reason}"

    # --- 通用 selector 解析: 支持 'label:文字' ---
    def _resolve_locator(self, selector: str):
        page = self._page
        if selector.startswith("label:"):
            text = selector[len("label:"):].strip()
            return page.get_by_label(text, exact=False)
        if selector.startswith("text:"):
            text = selector[len("text:"):].strip()
            return page.get_by_text(text, exact=False)
        if selector.startswith("role:"):
            # role:button:Apply
            parts = selector[len("role:"):].split(":", 1)
            role = parts[0]
            name = parts[1] if len(parts) > 1 else None
            return page.get_by_role(role, name=name)
        return page.locator(selector).first

    # --- Tool 路由 ---
    def dispatch(self, name: str, args: dict) -> str:
        try:
            if name == "read_page":
                return self.tool_read_page()
            if name == "fill_field":
                return self.tool_fill(args["selector"], args["value"])
            if name == "select_option":
                return self.tool_select(args["selector"], args["value"])
            if name == "click":
                return self.tool_click(args["selector"])
            if name == "upload_file":
                return self.tool_upload(args["selector"], args["path"])
            if name == "screenshot":
                return self.tool_screenshot()
            if name == "ready_to_submit":
                return self.tool_ready_to_submit(args["summary"], args["submit_selector"])
            if name == "give_up":
                return self.tool_give_up(args["reason"])
            return f"未知工具: {name}"
        except Exception as e:
            return f"工具 {name} 执行出错: {e}"


def assist_apply(config: Config, job_id: int, auto_submit: bool | None = None) -> dict:
    """主入口: 跑一次半自动投递."""
    from playwright.sync_api import sync_playwright

    apply_cfg = config.apply_settings
    if auto_submit is None:
        auto_submit = bool(apply_cfg.get("auto_submit", False))

    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job #{job_id} 不存在")
        if not job.tailored_resume_path:
            raise RuntimeError("请先跑 `tailor --job-id N` 生成定制简历")
        cover_letter_text = (
            Path(job.cover_letter_path).read_text() if job.cover_letter_path else ""
        )
        # 拷贝出来,等会儿 session 关掉还能用
        job_snapshot = {
            "id": job.id,
            "url": job.url,
            "title": job.title,
            "company": job.company,
            "description": job.description or "",
            "tailored_resume_path": job.tailored_resume_path,
        }

    candidate = apply_cfg.get("candidate", {})
    system_prompt = render(
        load_prompt("apply_system"),
        candidate_json=json.dumps(candidate, ensure_ascii=False, indent=2),
        resume_path=job_snapshot["tailored_resume_path"],
        cover_letter=cover_letter_text or "(未生成)",
        title=job_snapshot["title"],
        company=job_snapshot["company"],
        job_description=(job_snapshot["description"] or "")[:2000],
    )

    client, model_name = make_client(config, "apply")
    sess = ApplySession(config, Job(**{
        k: v for k, v in job_snapshot.items() if k in {"id", "title", "company", "url"}
    }), auto_submit=auto_submit)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=80)
        # 加载 cookies (尝试匹配 source 平台)
        ctx = browser.new_context()
        try:
            from .auth import load_cookies
            from urllib.parse import urlparse
            host = urlparse(job_snapshot["url"]).netloc
            for plat in ["linkedin", "indeed", "glassdoor", "ziprecruiter"]:
                if plat in host:
                    ctx.add_cookies(load_cookies(config, plat))
                    break
        except Exception:
            pass

        page = ctx.new_page()
        page.goto(job_snapshot["url"], wait_until="domcontentloaded", timeout=45000)
        sess._page = page

        messages: list[dict] = [
            {"role": "user", "content": "请开始投递这个岗位."}
        ]

        max_turns = 25
        for turn in range(max_turns):
            resp = client.messages.create(
                model=model_name,
                max_tokens=2048,
                system=system_prompt,
                tools=APPLY_TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": resp.content})

            # 找 tool_use
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                # 没有工具调用 = 结束
                break

            tool_results = []
            for tu in tool_uses:
                print(f"[turn {turn}] {tu.name}({json.dumps(tu.input, ensure_ascii=False)[:120]})")
                result = sess.dispatch(tu.name, tu.input or {})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result[:2000],
                    }
                )

            messages.append({"role": "user", "content": tool_results})

            if sess._submit_done or sess._given_up_reason:
                break

        browser.close()

    # 落库
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if sess._submit_done:
            job.status = JobStatus.APPLIED.value
            job.applied_at = datetime.utcnow()
            session.add(Event(job_id=job_id, kind="applied", content="agent 投递"))
        elif sess._given_up_reason:
            session.add(
                Event(job_id=job_id, kind="apply_failed", content=sess._given_up_reason)
            )
        session.commit()

    return {
        "submitted": sess._submit_done,
        "given_up": sess._given_up_reason,
    }


def mark_applied(config: Config, job_id: int, note: str | None = None) -> None:
    """手动标记某岗位为已投递,记录时间戳."""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} 不存在")
        job.status = JobStatus.APPLIED.value
        job.applied_at = datetime.utcnow()
        session.add(Event(job_id=job_id, kind="applied", content=note))
        session.add(job)
        session.commit()
