#!/usr/bin/env bash
# 每日 cron 入口: 借鉴 n8n 工作流的 Schedule Trigger
#
# 安装方法 (Linux/macOS):
#   crontab -e
# 然后加一行 (每天早上 7:30 跑):
#   30 7 * * *  /home/johnny/Documents/Claude/Projects/job-agent/scripts/daily.sh >> /tmp/job-agent.log 2>&1

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# 跑全流程
echo "=== $(date) Job Agent daily run ==="
python3 -m src.main run-all
echo "=== Done $(date) ==="
