#!/usr/bin/env bash
# 每日 cron 入口
#
# 安装方法 (Linux/macOS):
#   crontab -e
# 然后加一行 (每天早上 7:30):
#   30 7 * * *  /home/johnny/Documents/Claude/Projects/job-agent/scripts/daily.sh >> /tmp/job-agent.log 2>&1

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 激活 conda 环境 (cron 启动时 PATH 不含 conda)
CONDA_BASE="${CONDA_BASE:-/home/johnny/miniconda3}"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate job-agent

echo "=== $(date) JobHunter daily run ==="
export JOBHUNTER_TRIGGER=cron
python -m src.main run-all
echo "=== Done $(date) ==="
