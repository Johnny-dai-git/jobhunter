"""Manage user crontab — true 24/7 background automation.

Difference from in-process scheduler:
- in-process: only works while web server is running (stops when it stops)
- system cron: OS-level, always runs, continues even after reboot

Implementation: use subprocess to call `crontab -l` for read / `crontab -` for write,
use # JOBHUNTER comment line as marker, convenient for future update / uninstall.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


MARKER = "# JOBHUNTER managed"

# Hour interval -> cron expression
HOURS_TO_CRON = {
    1:   "0 * * * *",      # Every hour on the hour
    3:   "0 */3 * * *",    # Every 3 hours
    6:   "0 */6 * * *",    # Every 6 hours
    12:  "0 */12 * * *",   # Every 12 hours
    24:  "30 7 * * *",     # Daily at 07:30
    168: "30 7 * * 0",     # Every Sunday at 07:30
}


def crontab_available() -> bool:
    """Check if system has crontab CLI (Linux/macOS yes, Windows no)."""
    try:
        r = subprocess.run(["crontab", "-h"], capture_output=True, timeout=5)
        # crontab has no -h, but it will exit. Key is command exists
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _read_crontab() -> str:
    """Read current user's crontab. Return '' if none."""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        # Exit code 1 + 'no crontab for ...' is normal, means empty
        if r.returncode == 0:
            return r.stdout
        return ""
    except Exception:
        return ""


def _write_crontab(content: str) -> None:
    """Write crontab (overwrite). Raises exception on failure."""
    proc = subprocess.Popen(
        ["crontab", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout, stderr = proc.communicate(content, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab write failed: {stderr.strip()}")


def get_status() -> dict:
    """Return {available, installed, line, hours}."""
    if not crontab_available():
        return {"available": False, "installed": False, "line": None, "hours": None}
    content = _read_crontab()
    for line in content.splitlines():
        if MARKER in line:
            # Reverse-lookup interval hours (approximate)
            hours = None
            for h, expr in HOURS_TO_CRON.items():
                if line.startswith(expr):
                    hours = h
                    break
            return {"available": True, "installed": True, "line": line.strip(), "hours": hours}
    return {"available": True, "installed": False, "line": None, "hours": None}


def install(hours: int, script_path: Path, log_path: str = "/tmp/jobhunter.log") -> str:
    """Add (or replace) JobHunter's line in user crontab. Return new line."""
    if hours not in HOURS_TO_CRON:
        raise ValueError(
            f"Unsupported interval {hours}h. Options: {sorted(HOURS_TO_CRON.keys())}"
        )
    if not crontab_available():
        raise RuntimeError("Current system has no crontab command (Windows?)")

    cron_expr = HOURS_TO_CRON[hours]
    existing = _read_crontab()
    # Delete old marked line
    lines = [l for l in existing.splitlines() if MARKER not in l]
    new_line = f"{cron_expr} {script_path} >> {log_path} 2>&1 {MARKER}"
    lines.append(new_line)
    new_content = "\n".join(lines).rstrip() + "\n"
    _write_crontab(new_content)
    return new_line


def uninstall() -> bool:
    """Uninstall JobHunter's line. Return success (also True if not installed)."""
    if not crontab_available():
        return False
    existing = _read_crontab()
    if MARKER not in existing:
        return True
    lines = [l for l in existing.splitlines() if MARKER not in l]
    new_content = "\n".join(lines).rstrip() + ("\n" if lines else "")
    try:
        _write_crontab(new_content)
        return True
    except Exception:
        return False
