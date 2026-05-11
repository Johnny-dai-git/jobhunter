# Job Agent

一个基于 DeepSeek V4 的个人求职助手. **不自动投递,只发邮件**.

## 它能做什么

- **自动采集**: 每天定时从 LinkedIn / Indeed / Glassdoor / ZipRecruiter 抓最近 24h 的新岗位
- **6 维度智能评分**: 背景 / 技能 / 经验 / 资历 / 工作授权 / 公司类型 — 每岗位返回可解释的子分
- **关键词预过滤**: 采集时就剔除 intern/student/postdoc 等明显不符合的岗位,省 LLM 钱
- **简历定制**: 针对感兴趣的岗位重写简历重点
- **求职信生成**: 复用 matcher 提取的 connector / fit_bullets,一次生成针对性 cover letter
- **每日邮件**: Top-N 高匹配岗位拼成 HTML digest 发到你信箱
- **市场趋势报告**: 主要 Player / 技术栈热度 / 薪资水位 / 给你的具体建议
- **手动投递追踪**: 你在网站上自己投完之后跑 `mark-applied` 记录状态

**不做的事**: 自动填表 / 自动点击提交按钮 / 任何不可逆动作.

---

## 快速开始

### 1. 安装

```bash
cd job-agent
conda create -n job-agent python=3.12 -y
conda activate job-agent
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env, 填 DEEPSEEK_API_KEY
# 如果要发邮件 digest, 再填 SMTP_PASSWORD
```

编辑 `config.yaml`:
- `preferences.job_titles` / `locations`: 要搜什么岗位
- `preferences.exclude_keywords`: 不想看的关键词
- `digest.to`: 收件邮箱

把简历 PDF/DOCX 放进 `data/resume/`.

### 3. 一次性准备

```bash
python -m src.main init                                 # 建数据库
python -m src.main parse-resume                         # 解析简历

# 登录各招聘平台 (会打开浏览器, 手动登一下, 自动保存 cookies)
python -m src.main login --platform linkedin
python -m src.main login --platform indeed
python -m src.main login --platform ziprecruiter
python -m src.main login --platform glassdoor
```

### 4. 跑起来

**单步**:
```bash
python -m src.main collect                # 采集所有平台
python -m src.main match                  # 6 维度评分
python -m src.main list --min-score 70    # 看高分
python -m src.main show --job-id 5        # 看详情 (含子分 + fit_bullets + connector)
python -m src.main tailor --job-id 5      # 给 #5 生成定制简历 + 求职信
python -m src.main mark-applied --job-id 5 --note "通过公司官网投递"
python -m src.main digest                 # 生成今日 digest 邮件
python -m src.main trends --email         # 生成趋势报告并邮件发送
```

**一键全流程** (不含投递):
```bash
python -m src.main run-all
# 流程: collect → match → digest 邮件 → 趋势报告邮件
```

**每天自动跑** (cron):
```bash
crontab -e
# 加一行 (每天 7:30):
30 7 * * *  /home/johnny/Documents/Claude/Projects/job-agent/scripts/daily.sh >> /tmp/job-agent.log 2>&1
# 加一行 (每周日 9 点跑趋势分析):
0 9 * * 0   /home/johnny/Documents/Claude/Projects/job-agent/scripts/weekly.sh >> /tmp/job-agent.log 2>&1
```

---

## 命令速查

| 命令 | 用途 |
|---|---|
| `init` | 建数据库和目录 |
| `parse-resume` | 解析简历缓存 |
| `login --platform X` | 登录某平台保存 cookies |
| `collect [--platform X]` | 采集岗位 (过滤排除关键词) |
| `add-job` | 手动加一个岗位 |
| `match` | 6 维度评分 |
| `list [--min-score N]` | 查看追踪表 |
| `show --job-id N` | 看某岗位详情 (含 6 维度子分) |
| `tailor --job-id N` | 生成定制简历 + 求职信 |
| `mark-applied --job-id N` | 标记你手动投了 |
| `digest` | 生成今日 HTML digest |
| `trends [--email]` | 生成市场趋势报告 |
| `run-all` | 一键 collect → match → digest 邮件 → 趋势邮件 |

---

## 6 维度评分

借鉴 [DailyJobMatch](https://github.com/chunxubioinfor/DailyJobMatch) 的设计:

| 维度 | 满分 | 在评什么 |
|---|---|---|
| `background_match` | 10 | 行业/领域匹配度 |
| `skills_overlap` | 30 | 技术栈交集 (权重最高) |
| `experience_relevance` | 30 | 经验和岗位职责对齐度 (权重最高) |
| `seniority` | 10 | 资历层级匹配 |
| `authorization` | 10 | 工作授权/签证匹配 |
| `company_score` | 10 | 公司类型偏好匹配 |
| **`overall`** | **100** | 加权总分 |

`show --job-id N` 会用表格显示每个子分,让你知道为什么 78 分 (而不是 95)。

Matcher 同时返回:
- **`keywords`** — JD 关键词,ATS 优化用
- **`fit_bullets`** — 3-5 条"为什么 fit"的子弹点,直接复用到求职信第二段
- **`connector`** — 一句话钩子,直接作为求职信第一句

这样 `tailor` 命令生成求职信时就不用重新问 LLM 这些问题,省一次调用.

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
│   ├── daily.sh             # cron 入口 (每日)
│   └── weekly.sh            # cron 入口 (每周)
├── src/
│   ├── main.py              # CLI
│   ├── config.py
│   ├── db.py                # SQLite (Job + Event)
│   ├── auth.py              # 各平台登录 + cookie 保存
│   ├── resume_reader.py
│   ├── agent.py             # LLM 调用工厂 (Claude / DeepSeek 共用)
│   ├── matcher.py           # 6 维度评分 (tool_use 结构化输出)
│   ├── tailor.py            # 简历定制
│   ├── cover_letter.py      # 求职信生成 (复用 matcher 的 connector/fit_bullets)
│   ├── tracker.py           # 手动投递状态标记
│   ├── digest.py            # 每日 HTML 邮件
│   ├── trends.py            # 市场趋势分析
│   ├── collect.py           # 采集编排 (含 exclude_keywords 过滤)
│   └── collectors/
│       ├── _browser.py
│       ├── linkedin.py
│       ├── indeed.py
│       ├── glassdoor.py
│       └── ziprecruiter.py
├── prompts/
│   ├── matcher.md           # 6 维度评分 prompt
│   ├── tailor.md
│   ├── cover_letter.md      # 复用 connector/fit_bullets 的 prompt
│   └── trends.md
└── data/
    ├── resume/              # 你的简历放这儿
    ├── cookies/             # 平台登录态 (gitignore)
    ├── jobs/
    ├── outputs/             # 生成的简历/求职信/digest/趋势报告
    └── jobs.db
```

---

## 重要提示

- **不自动投递**: 这是设计选择. 投错的简历删不掉. 邮件看完后你自己去网站投, 跑 `mark-applied` 记录.
- **遵守平台 ToS**: 直接爬 LinkedIn 等平台风险存在. 本工具用"已登录用户视角" + 限速 + 限量 (默认 30 个/平台/次).
- **API 成本**: 全 DeepSeek 路由,一天约 ¥1-2.
- **隐私**: 简历内容会发到 DeepSeek API. cookies 只在本地. 数据库里没有密码.
- **选择器会失效**: 各平台经常改 DOM. 如果 collector 抓不到东西, 按 collector 文件里的 selector 适配最新页面结构.
