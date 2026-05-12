"""In-process scheduled task: check every minute, call callback when time comes.

Design:
- No external dependencies (no apscheduler needed)
- Configuration persisted to data/settings.json
- Only works when web server running (stops when server stops)
- Main pipeline: runs every 24 hours (collect + match + digest)
- Trends: runs every 7 days (market trends report + email)
- Prevent re-entry: checked via pipeline_state.running inside callback

Callback signature: callback(run_trends: bool) -> None
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional


MAIN_INTERVAL_HOURS   = 24    # Main pipeline: collect + match + digest
TRENDS_INTERVAL_HOURS = 168   # Trends report: every 7 days


class AgentScheduler:
    def __init__(self, settings_path: Path, callback: Callable[[bool], None]):
        """callback(run_trends: bool) — run_trends=True means also run trends this time."""
        self.settings_path = settings_path
        self.callback = callback
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # ── Persistence ─────────────────────────────────────────────────────────────

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

    # ── Public API ────────────────────────────────────────────────────────────

    def get_schedule_hours(self) -> int:
        """Main pipeline interval (0 = disabled)."""
        return int(self._load().get("schedule_hours", 0) or 0)

    def set_schedule_hours(self, hours: int) -> None:
        hours = max(0, int(hours))
        s = self._load()
        s["schedule_hours"] = hours
        self._save(s)

    def enable(self, hours: int = MAIN_INTERVAL_HOURS) -> None:
        """Called when activating profile — set interval and reset timing.
        hours>0: clear last_auto_run so next loop triggers immediately.
        hours=0: manual mode, also clear last_auto_run to keep state clean.
        """
        s = self._load()
        s["schedule_hours"] = max(0, int(hours))
        s.pop("last_auto_run", None)   # Always clear to keep state consistent
        self._save(s)

    def disable(self) -> None:
        s = self._load()
        s["schedule_hours"] = 0
        self._save(s)

    # Main pipeline last run time
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

    # Trends last run time
    def get_last_trends_run(self) -> Optional[datetime]:
        v = self._load().get("last_trends_run")
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except (ValueError, TypeError):
            return None

    def should_run_trends(self) -> bool:
        """Return True if more than 7 days since last trends."""
        last = self.get_last_trends_run()
        if not last:
            return True   # Never run trends before → run this time
        return (datetime.now() - last) >= timedelta(hours=TRENDS_INTERVAL_HOURS)

    # ── Background loop ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="AgentScheduler")
        self.thread.start()
        print("[scheduler] Started")

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        """Check every 60s, trigger when time comes."""
        while not self.stop_event.wait(60):
            try:
                hours = self.get_schedule_hours()
                if hours <= 0:
                    continue

                last = self.get_last_run()
                if last and (datetime.now() - last) < timedelta(hours=hours):
                    continue

                run_trends = self.should_run_trends()
                print(f"[scheduler] Triggered (interval={hours}h, trends={run_trends})")
                try:
                    self.callback(run_trends)
                except Exception as e:
                    print(f"[scheduler] Callback failed: {e}")
                    traceback.print_exc()

                self.mark_ran(include_trends=run_trends)

            except Exception:
                traceback.print_exc()
