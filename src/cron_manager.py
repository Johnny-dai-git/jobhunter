"""管理用户 crontab — 真正的 24/7 后台自动跑.

跟 in-process scheduler 区别:
- in-process: 只在 web server 运行时生效 (它停了就停)
- system cron: 操作系统级别, 永远跑, 即使关电脑只要重开它接着跑

实现: 用 subprocess 调 `crontab -l` 读 / `crontab -` 写, 通过 # JOBHUNTER 注释行
作为我们这行的标记, 方便后续 update / uninstall.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


MARKER = "# JOBHUNTER managed"

# 小时间隔 -> cron expression
HOURS_TO_CRON = {
    1:   "0 * * * *",      # 每小时整点
    3:   "0 */3 * * *",    # 每 3 小时
    6:   "0 */6 * * *",    # 每 6 小时
    12:  "0 */12 * * *",   # 每 12 小时
    24:  "30 7 * * *",     # 每天 07:30
    168: "30 7 * * 0",     # 每周日 07:30
}


def crontab_available() -> bool:
    """检查系统是否有 crontab CLI (Linux/macOS yes, Windows no)."""
    try:
        r = subprocess.run(["crontab", "-h"], capture_output=True, timeout=5)
        # crontab 没有 -h, 但它会 exit. 关键是命令存在
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _read_crontab() -> str:
    """读当前用户的 crontab. 没有就返回 ''."""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        # exit code 1 + 'no crontab for ...' 是正常的, 表示空
        if r.returncode == 0:
            return r.stdout
        return ""
    except Exception:
        return ""


def _write_crontab(content: str) -> None:
    """覆盖写 crontab. 失败抛异常."""
    proc = subprocess.Popen(
        ["crontab", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout, stderr = proc.communicate(content, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab 写失败: {stderr.strip()}")


def get_status() -> dict:
    """返回 {available, installed, line, hours}."""
    if not crontab_available():
        return {"available": False, "installed": False, "line": None, "hours": None}
    content = _read_crontab()
    for line in content.splitlines():
        if MARKER in line:
            # 反查间隔小时数 (粗略)
            hours = None
            for h, expr in HOURS_TO_CRON.items():
                if line.startswith(expr):
                    hours = h
                    break
            return {"available": True, "installed": True, "line": line.strip(), "hours": hours}
    return {"available": True, "installed": False, "line": None, "hours": None}


def install(hours: int, script_path: Path, log_path: str = "/tmp/jobhunter.log") -> str:
    """加 (或替换) JobHunter 在用户 crontab 中的那一行. 返回新行."""
    if hours not in HOURS_TO_CRON:
        raise ValueError(
            f"不支持的间隔 {hours}h. 可选: {sorted(HOURS_TO_CRON.keys())}"
        )
    if not crontab_available():
        raise RuntimeError("当前系统没有 crontab 命令 (Windows?)")

    cron_expr = HOURS_TO_CRON[hours]
    existing = _read_crontab()
    # 删旧标记行
    lines = [l for l in existing.splitlines() if MARKER not in l]
    new_line = f"{cron_expr} {script_path} >> {log_path} 2>&1 {MARKER}"
    lines.append(new_line)
    new_content = "\n".join(lines).rstrip() + "\n"
    _write_crontab(new_content)
    return new_line


def uninstall() -> bool:
    """卸载 JobHunter 那一行. 返回是否成功 (没装就也返回 True)."""
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
