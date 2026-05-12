"""进程内定时调度: 每分钟检查一次, 到时间就调 callback.

设计:
- 不依赖外部库 (无需 apscheduler 等)
- 配置持久化到 data/settings.json
- 仅在 web server 运行时生效 (server 关掉调度也停)
- 主流水线: 每 24 小时跑一次 (collect + match + digest)
- Trends: 每 7 天跑一次 (市场趋势报告 + 邮件)
- 防重入: 通过 callback 内部 pipeline_state.running 判断

callback 签名: callback(run_trends: bool) -> None
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional


MAIN_INTERVAL_HOURS   = 24    # 主流水线: collect + match + digest
TRENDS_INTERVAL_HOURS = 168   # 趋势报告: 每 7 天


class AgentScheduler:
    def __init__(self, settings_path: Path, callback: Callable[[bool], None]):
        """callback(run_trends: bool) — run_trends=True 时本次额外跑 trends."""
        self.settings_path = settings_path
        self.callback = callback
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # ── 持久化 ─────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.settings_path.exists():
            try:
                return json.loads(self.settings_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self, s: dict) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")

    # ── 公共 API ────────────────────────────────────────────────────────────

    def get_schedule_hours(self) -> int:
        """主流水线间隔 (0 = 关闭)."""
        return int(self._load().get("schedule_hours", 0) or 0)

    def set_schedule_hours(self, hours: int) -> None:
        hours = max(0, int(hours))
        s = self._load()
        s["schedule_hours"] = hours
        self._save(s)

    def enable(self, hours: int = MAIN_INTERVAL_HOURS) -> None:
        """激活画像时调用 — 设定间隔并重置计时。
        hours>0: 清 last_auto_run 让下次循环立即触发。
        hours=0: 手动模式，也清 last_auto_run 保持状态干净。
        """
        s = self._load()
        s["schedule_hours"] = max(0, int(hours))
        s.pop("last_auto_run", None)   # 始终清除，保持状态一致
        self._save(s)

    def disable(self) -> None:
        s = self._load()
        s["schedule_hours"] = 0
        self._save(s)

    # 主流水线上次运行时间
    def get_last_run(self) -> Optional[datetime]:
        v = self._load().get("last_auto_run")
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except (ValueError, TypeError):
            return None

    def get_next_run(self) -> Optional[datetime]:
        hours = self.get_schedule_hours()
        if hours <= 0:
            return None
        last = self.get_last_run()
        if not last:
            return datetime.now()
        return last + timedelta(hours=hours)

    def mark_ran(self, include_trends: bool = False) -> None:
        s = self._load()
        now = datetime.now().isoformat()
        s["last_auto_run"] = now
        if include_trends:
            s["last_trends_run"] = now
        self._save(s)

    # Trends 上次运行时间
    def get_last_trends_run(self) -> Optional[datetime]:
        v = self._load().get("last_trends_run")
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except (ValueError, TypeError):
            return None

    def should_run_trends(self) -> bool:
        """距上次 trends 超过 7 天则返回 True."""
        last = self.get_last_trends_run()
        if not last:
            return True   # 从来没跑过 trends → 这次一起跑
        return (datetime.now() - last) >= timedelta(hours=TRENDS_INTERVAL_HOURS)

    # ── 后台循环 ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="AgentScheduler")
        self.thread.start()
        print("[scheduler] 已启动")

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        """每 60s 检查一次，到点就触发。"""
        while not self.stop_event.wait(60):
            try:
                hours = self.get_schedule_hours()
                if hours <= 0:
                    continue

                last = self.get_last_run()
                if last and (datetime.now() - last) < timedelta(hours=hours):
                    continue

                run_trends = self.should_run_trends()
                print(f"[scheduler] 触发 (interval={hours}h, trends={run_trends})")
                try:
                    self.callback(run_trends)
                except Exception as e:
                    print(f"[scheduler] callback 失败: {e}")
                    traceback.print_exc()

                self.mark_ran(include_trends=run_trends)

            except Exception:
                traceback.print_exc()
