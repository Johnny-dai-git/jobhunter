# Job Agent

一个基于 Claude Agent SDK 的个人求职助手 (全自动版).

## 它能做什么

- **自动采集**: 每天定时从 LinkedIn / Indeed / Glassdoor / ZipRecruiter 抓最近 24h 的新岗位
- **智能评分**: 用 Claude (tool_use 强制结构化输出) 给每个岗位打分,生成命中点 + 差距分析
- **简历定制**: 针对每个岗位重写简历重点 (不捏造经历)
- **求职信生成**: 基于 JD 写真针对性的 cover letter
- **半自动投递**: Claude 看页面、调 Playwright 自己填表,默认在提交按钮前暂停等你确认
- **每日 digest**: 把 Top-N 高匹配岗位拼成漂亮 HTML 邮件发到你信箱
- **市场趋势分析**: 基于已采集岗位生成趋势报告 — 主要 player / 技术栈热度 / 薪资水位 / 给你的具体建议
- **追踪面板**: SQLite 记录每个岗位的状态、评分、生成产物路径

借鉴了 n8n 工作流的设计(定时触发 / 24h 时间窗 / 结构化输出 / 邮件 digest),用 Python + Claude tool_use 实现.

---

## 快速开始

### 1. 安装

```bash
cd job-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env, 填 ANTHROPIC_API_KEY
# 如果要发邮件 digest,再加 SMTP_PASSWORD
```

编辑 `config.yaml`:
- `preferences.job_titles` / `locations`: 要搜什么岗位
- `digest.to`: 收件邮箱
- `apply.candidate`: 投递时用的姓名/邮箱/电话/工作授权等

把简历 PDF/DOCX 放进 `data/resume/`.

### 3. 一次性准备

```bash
# 初始化数据库
python3 -m src.main init

# 解析简历
python3 -m src.main parse-resume

# 登录各招聘平台 (会打开浏览器,手动登一下,自动保存 cookies)
python3 -m src.main login --platform linkedin
python3 -m src.main login --platform indeed
python3 -m src.main login --platform ziprecruiter
python3 -m src.main login --platform glassdoor
```

### 4. 跑起来

**手动单步**:
```bash
python3 -m src.main collect              # 采集所有平台
python3 -m src.main match                # 评分
python3 -m src.main list --min-score 70  # 看高分
python3 -m src.main tailor --job-id 5    # 给 #5 生成简历+求职信
python3 -m src.main apply --job-id 5     # 半自动投 #5
python3 -m src.main digest               # 生成今日 digest
```

**一键全流程**:
```bash
python3 -m src.main run-all
```

**每天定时跑** (cron):
```bash
crontab -e
# 加一行:
30 7 * * *  /home/johnny/Documents/Claude/Projects/job-agent/scripts/daily.sh >> /tmp/job-agent.log 2>&1
```

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `init` | 建数据库和目录 |
| `parse-resume` | 解析简历缓存 |
| `login --platform X` | 登录某平台保存 cookies |
| `collect [--platform X]` | 采集岗位 |
| `add-job` | 手动加一个岗位 |
| `match` | 给未评分的打分 |
| `list [--min-score N]` | 查看追踪表 |
| `show --job-id N` | 看某岗位详情 |
| `tailor --job-id N` | 生成定制简历+求职信 |
| `apply --job-id N [--auto-submit]` | 半自动投递 |
| `mark-applied --job-id N` | 手动标记已投 |
| `digest` | 生成今日 HTML digest |
| `trends [--days N] [--min-score N]` | 生成市场趋势报告 (主要 player / 技术栈 / 薪资 / 建议) |
| `run-all [--with-trends]` | 一键 collect → match → digest [→ trends] |

---

## 投递助手怎么工作的

1. 用 Playwright 打开应聘页 (Chromium 可见模式,你能看到全过程)
2. Claude 通过 tool_use 调下面的工具:
   - `read_page`: 看页面上有哪些 form 字段
   - `fill_field` / `select_option` / `click` / `upload_file`: 填表
   - `screenshot`: 不确定时截图自己看
   - `ready_to_submit`: 准备好提交,**默认会暂停问你 y/n**
   - `give_up`: 遇到不会的问题(coding test / 长篇 essay)就放弃
3. 全程在浏览器里发生,你随时能介入
4. `auto_submit=true` 才会真的自动点提交

为什么不用死写脚本? 因为不同 ATS (Greenhouse / Lever / Workday / Taleo) 表单千差万别,死脚本维护成本极高. 让 Claude 看页面理解字段含义,鲁棒得多.

---

## 项目结构

```
job-agent/
├── README.md
├── requirements.txt
├── .env.example
├── config.yaml
├── pyproject.toml
├── scripts/
│   └── daily.sh             # cron 入口
├── src/
│   ├── main.py              # CLI
│   ├── config.py
│   ├── db.py                # SQLite (Job + Event)
│   ├── auth.py              # 各平台登录 + cookie 保存
│   ├── resume_reader.py
│   ├── agent.py             # Claude 调用封装
│   ├── matcher.py           # 评分 (tool_use 结构化输出)
│   ├── tailor.py            # 简历定制
│   ├── cover_letter.py      # 求职信生成
│   ├── apply_assist.py      # 半自动投递 (tool_use + Playwright)
│   ├── digest.py            # 每日 HTML 邮件
│   ├── collect.py           # 采集编排器
│   └── collectors/
│       ├── _browser.py      # Playwright 共用
│       ├── linkedin.py
│       ├── indeed.py
│       ├── glassdoor.py
│       └── ziprecruiter.py
├── prompts/
│   ├── matcher.md
│   ├── tailor.md
│   ├── cover_letter.md
│   └── apply_system.md      # 投递助手 system prompt
└── data/
    ├── resume/              # 你的简历
    ├── cookies/             # 登录态 (gitignore)
    ├── jobs/
    ├── outputs/             # 生成的简历/求职信/截图/digest
    └── jobs.db
```

---

## 重要提示

- **遵守平台 ToS**: LinkedIn 等平台禁止自动化. 本工具用"已登录用户视角"+ 限速 + 限量 (默认 30 个/平台/次),风险可控. 大规模爬取请用官方 API.
- **API 成本**: 每次 match ~¥0.05, tailor + cover ~¥0.2, apply ~¥0.5–1. 一天跑下来通常 < ¥10.
- **隐私**: 简历内容会发到 Anthropic API. cookies 只在本地. 数据库里没有密码.
- **投递可逆性**: `apply` 默认在提交前暂停. `--auto-submit` 之后不可逆,慎用.
- **选择器会失效**: 各平台经常改 DOM. 如果 collector 抓不到东西,按 collector 文件里的 selector 适配最新页面结构.
