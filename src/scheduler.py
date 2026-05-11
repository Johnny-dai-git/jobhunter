"""进程内定时调度: 每分钟检查一次, 到时间就调 callback.

设计:
- 不依赖外部库 (无需 apscheduler 等)
- 配置持久化到 data/settings.json
- 仅在 web server 运行时生效 (server 关掉调度也停)
- 调度间隔: 0 = 关闭, 否则 N 小时 (1/6/12/24/168)
- 防重入: 通过 callback 内部 pipeline_state.running 判断

要做 24/7 后台跑, 需要单独配 crontab (用 scripts/daily.sh).
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional


class AgentScheduler:
    def __init__(self, settings_path: Path, callback: Callable[[], None]):
        self.settings_path = settings_path
        self.callback = callback
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # ---- 持久化 ----
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

    # ---- 公共 API ----
    def get_schedule_hours(self) -> int:
        return int(self._load().get("schedule_hours", 0) or 0)

    def set_schedule_hours(self, hours: int) -> None:
        hours = max(0, int(hours))
        s = self._load()
        s["schedule_hours"] = hours
        self._save(s)

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
            return datetime.now()  # 从来没跑过 -> 下次循环就跑
        return last + timedelta(hours=hours)

    def mark_ran(self) -> None:
        s = self._load()
        s["last_auto_run"] = datetime.now().isoformat()
        self._save(s)

    # ---- 后台循环 ----
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
        # 每 60s 检查一次
        while not self.stop_event.wait(60):
            try:
                hours = self.get_schedule_hours()
                if hours <= 0:
                    continue
                last = self.get_last_run()
                if last and (datetime.now() - last) < timedelta(hours=hours):
                    continue
                print(f"[scheduler] 到点了 (interval={hours}h, last={last}), 触发流水线")
                try:
                    self.callback()
                except Exception as e:
                    print(f"[scheduler] callback 失败: {e}")
                    traceback.print_exc()
                # 标记已运行 (无论 callback 成功与否,避免无限重试)
                self.mark_ran()
            except Exception:
                traceback.print_exc()
