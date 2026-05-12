# Profile Analyzer — Round 3: Top-10 输出

> 在这一步之前，你已经完成了：
> - **Round 1**: 从简历和资料库中提取了结构化技能档案和 ATS 关键词池
> - **Round 2**: 从 HR / HM / 策略师三个视角完成了候选人定位分析
>
> 现在基于以上全部上下文，输出最终的 Top-10 最优投递方向。

---

> 两段式策略: 第一步是**先锁定 10 个 primary** (精确高分), 第二步在 collect 阶段把每个 primary 的 aliases / broader_terms 一并展开 (模糊扩大命中面). 所以 primary 要选得**准且互不重叠**, 把"模糊扩散"的活留给 aliases.

## 评分维度
综合 Round 1 提取的技能档案和 Round 2 的三视角分析, 对每个方向评分:

| 维度 | 含义 | 范围 |
|---|---|---|
| `market_demand` | 该 title 在 LinkedIn/Indeed 上当前招聘量 | 0-10 (10 = 每天大量新岗) |
| `competition` | 跟候选人背景接近的求职者密度 | 0-10 (**越低越好**) |
| `user_advantage` | 候选人简历对此 title 的匹配深度 | 0-10 (10 = 几乎全命中) |

`composite = market_demand * (10 - competition) * user_advantage / 10`, 0-100.

## 硬约束

### Title 选择
- **本 agent 只做 industry / 工业界岗位**. 仅 2 个 direction 枚举:
  - `engineering` — 普通工程岗 (SWE, MLE, Backend, Platform, Infra, Data, Security, SRE, etc.)
  - `research-engineering` — 工业界研究工程 (Research Engineer / Member of Technical Staff at Anthropic / DeepMind / OpenAI / Mistral 类)
- **不要**产出任何学术岗 — 没有 Postdoc / Assistant Professor / TTAP / Lecturer / Research Associate (academic) / Adjunct 等. 哪怕用户提了, 也只产出最接近的 industry 替代 (例如 user 想做 research → 给工业界 Research Engineer / Research Scientist at Google/Meta/Anthropic, 而不是学校的 postdoc).
- **排除**: Manager / Director / VP / Head / Lead / PM / Designer / Sales / HR / Recruiter / Analyst (除非用户特别要求).
- **10 个 position 之间避免重复** (覆盖候选人不同侧面 / 不同 seniority / 不同细分方向).
- 10 个 primary **必须互相区分明显**, 不要给"ML Engineer / Machine Learning Engineer / ML Software Engineer"三个 primary — 这种属于 aliases 关系, 应该塞到同一个 primary 的 aliases 数组里.
- **不许捏造**简历里没有的经历或技能.

### Aliases (关键: 每个 position 必填 2-5 个)
**同一岗位在不同公司可能叫不同名字**. 你必须为每个 primary title 提供 2-5 个真实存在的同义/变体写法. 例如:

- "Machine Learning Engineer" 的 aliases:
  - "ML Engineer"
  - "Engineer, Machine Learning"
  - "ML Software Engineer"
  - "AI/ML Engineer"
  - "Machine Learning Software Engineer"

- "Research Engineer" 的 aliases:
  - "AI Research Engineer"
  - "Research Engineer, ML"
  - "Member of Technical Staff"  (Anthropic/OpenAI 常用)
  - "AI Researcher"

- "Site Reliability Engineer" 的 aliases:
  - "SRE"
  - "Production Engineer"
  - "DevOps Engineer"
  - "Platform Engineer"

Aliases **必须是市场上真实使用的**写法, 不许造词.

### Broader Terms (可选: 每个 position 0-3 个)
**有些公司用更广义的 title 但岗位实际就是这个方向**. 这是"隐藏机会":

- "Machine Learning Engineer" 在 Anthropic / OpenAI / Mistral 等 AI 公司常被叫:
  - "Software Engineer"  (隐藏的 ML 岗)
  - "Member of Technical Staff"

- "GPU Engineer" 在硬件公司常被叫:
  - "Engineer" (Nvidia)
  - "Compute Engineer"

只列**真的存在隐藏现象**的广义词. 如果没有就留空数组.

## 输出: 调用 `submit_profile_analysis` 工具

### top_10_positions: **恰好 10 个**, 按 composite 降序
每个 position:
- **title** (英文): primary, 最具体的 LinkedIn title (10 个 primary **彼此不重叠**)
- **direction**: `"engineering"` | `"research-engineering"` (只能选这 2 个, 工业界)
- **scores**: `{market_demand, competition, user_advantage, composite}`
- **aliases** (数组, 2-5 个): 真实使用的同义/变体 title (collect 会用这个模糊扩展)
- **broader_terms** (数组, 0-3 个): 可能隐藏此方向的广义 title (collect 也会用)
- **why_this_position** (2-5 条, 中文): 引用简历**具体项目/数字**, 不要空话
- **market_evidence** (中文, <= 50 字): 市场为啥旺
- **linkedin_search_url**: 用 primary title 的 LinkedIn 搜索 URL

### target_locations (3-5 个)

### recommended_companies (按 5 个区域分组)
基于你对市场和公司动态的认知,给出每个区域 **3-8 家最适合候选人背景且当前积极扩张** 的公司. 5 个区域:

- `north_america`: 美国 / 加拿大 (湾区、纽约、西雅图、波士顿、多伦多、温哥华等)
- `hong_kong`: 香港 (公司或在港办公室)
- `singapore`: 新加坡 (公司或在新办公室)
- `japan`: 日本 (东京 / 京都 / 大阪)
- `europe`: 欧洲发达国家 (英国 / 法国 / 德国 / 荷兰 / 瑞士 / 北欧)

**评判标准** (公司要满足):
1. **背景匹配**: 公司技术栈 / 产品方向 / 团队需求与候选人简历强相关
2. **积极扩张**: 最近 12 个月有以下信号之一:
   - 大额融资 / 上市 / 估值跃升
   - 新设地区办公室
   - 公开宣布大规模招聘
   - 大量职位挂在 LinkedIn

每家公司给出:
- **name** (公司名)
- **why_fit** (中文,<= 50 字): 为什么对候选人 fit, 引用简历项目或技术栈
- **hiring_signal** (中文,<= 50 字): 当前扩张/招聘信号(融资额、新办公室、岗位数等)
- **example_roles** (1-3 个,英文): 候选人可能投的具体岗位 title 例子
- **careers_url** (可选): 招聘页 URL

⚠️ 注意:
- 不许编造公司. 只列你确实知道的真实公司
- 如果某区域你确实不掌握合适的, 可以少给 (最少 3 家, 最多 8 家)
- 优先列对候选人技术栈匹配最深的, 不要堆数量

### summary (中文, <= 80 字)

---

## 候选人自述求职需求（最高优先级）
> {{ user_description }}

请把上面这段需求转化到 top_10_positions / target_locations / recommended_companies 中，优先尊重其中的方向/地点/公司类型偏好。

## 候选人偏好（系统默认，仅作 fallback）
{{ preferences }}

---

在 `why_this_position` 字段中，**必须**体现两层视角：
- 前 2 条以 `[HR]` 开头：关键词命中率、title 匹配、门槛是否满足
- 后 2-4 条以 `[HM]` 开头：引用 Round 1 中提取的具体项目/数字/论文/开源贡献作为技术深度佐证

只通过 `submit_profile_analysis` 返回结构化结果.
