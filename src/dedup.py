"""跨平台岗位去重: 公司名 + 标准化 title + location 的哈希.

解决的问题:
  - 同一岗位在 LinkedIn + Indeed + Dice 各发一次 → 3 条重复
  - 同公司用不同 title 写法发同一岗位 ("ML Engineer" vs "Machine Learning Engineer")

哈希策略:
  content_hash = sha256(normalize(company) + "|" + normalize(title) + "|" + normalize(location))[:16]

只要三个字段标准化后相同, 不同平台/不同 URL 的岗位就会被认为是重复.

标准化规则:
  company:  小写、去除法律后缀 (Inc/Corp/LLC...)、去标点
  title:    小写、去掉资历前缀 (Senior/Lead/Staff...)、统一常见缩写 (ML/SRE/SWE...)、去等级后缀 (L5/E4/I/II)
  location: 小写、统一城市别名 (SF→san francisco)、只保留城市名
"""
from __future__ import annotations

import hashlib
import re


# ── 公司名标准化 ─────────────────────────────────────────────────────────────

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|corp|ltd|llc|co|company|companies|technologies|technology|"
    r"tech|labs|laboratory|laboratories|ai|systems|solutions|group|"
    r"holdings|ventures|global|international|services|software|"
    r"consulting|consulting|networks|network|media|studio|studios)\b\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = _COMPANY_SUFFIXES.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s


# ── 职位名标准化 ─────────────────────────────────────────────────────────────

# 资历/级别前缀 (去掉)
_SENIORITY_PREFIX = re.compile(
    r"^(senior|sr\.?|junior|jr\.?|lead|staff|principal|associate|assoc\.?|"
    r"distinguished|fellow|founding|head of|director of|vp of|"
    r"entry.?level|mid.?level)\s+",
    re.IGNORECASE,
)

# 等级后缀: (L5), E4, I, II, III, IV, 1, 2, 3
_LEVEL_SUFFIX = re.compile(
    r"\s*[\(\[]?(l\d|e\d|m\d|t\d|\biv\b|\biii\b|\bii\b|\bi\b|\b[1-6]\b)[\)\]]?\s*$",
    re.IGNORECASE,
)

# 括号内容 (通常是补充说明或级别)
_PARENS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")

# 常见缩写统一 → 展开形式
_ABBREV: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bml\b",  re.I), "machine learning"),
    (re.compile(r"\bmle\b", re.I), "machine learning engineer"),
    (re.compile(r"\bsre\b", re.I), "site reliability engineer"),
    (re.compile(r"\bswe\b", re.I), "software engineer"),
    (re.compile(r"\bsde\b", re.I), "software development engineer"),
    (re.compile(r"\bai\b",  re.I), "artificial intelligence"),
    (re.compile(r"\bnlp\b", re.I), "natural language processing"),
    (re.compile(r"\bcv\b",  re.I), "computer vision"),
    (re.compile(r"\bllm\b", re.I), "large language model"),
    (re.compile(r"\binfra\b", re.I), "infrastructure"),
    (re.compile(r"\bdev\b",   re.I), "developer"),
    (re.compile(r"\beng\b",   re.I), "engineer"),
    (re.compile(r"\bmts\b",   re.I), "member technical staff"),
]


def normalize_title(title: str) -> str:
    if not title:
        return ""
    s = title.strip()
    # 去括号内容
    s = _PARENS.sub("", s)
    # 去等级后缀
    s = _LEVEL_SUFFIX.sub("", s)
    # 去资历前缀 (多次循环直到没有为止)
    for _ in range(3):
        new = _SENIORITY_PREFIX.sub("", s).strip()
        if new == s:
            break
        s = new
    # 小写
    s = s.lower()
    # 统一缩写
    for pattern, replacement in _ABBREV:
        s = pattern.sub(replacement, s)
    # 去多余空白和标点
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s


# ── 地点标准化 ────────────────────────────────────────────────────────────────

# 城市别名 → 标准名
_CITY_ALIASES: dict[str, str] = {
    "sf":              "san francisco",
    "bay area":        "san francisco",
    "silicon valley":  "san francisco",
    "nyc":             "new york",
    "new york city":   "new york",
    "ny":              "new york",
    "la":              "los angeles",
    "socal":           "los angeles",
    "seattle":         "seattle",
    "boston":          "boston",
    "chicago":         "chicago",
    "austin":          "austin",
    "denver":          "denver",
    "dc":              "washington dc",
    "washington dc":   "washington dc",
}

# 去掉州名/国家名后缀
_LOCATION_CLEANUP = re.compile(
    r",?\s*(ca|ny|wa|tx|ma|il|co|va|ga|fl|or|nc|az|mn|oh|"
    r"california|new york|washington|texas|massachusetts|illinois|"
    r"colorado|virginia|georgia|florida|oregon|united states|usa|us)\s*$",
    re.IGNORECASE,
)


def normalize_location(location: str) -> str:
    if not location:
        return "unknown"
    s = location.lower().strip()
    # remote 直接标准化
    if re.search(r"\bremote\b", s):
        return "remote"
    # 去州名/国家后缀
    s = _LOCATION_CLEANUP.sub("", s).strip(" ,")
    # 城市别名
    for alias, standard in _CITY_ALIASES.items():
        if s == alias or s.startswith(alias + " ") or s.startswith(alias + ","):
            return standard
    # 只保留第一个逗号前的部分 (城市)
    s = s.split(",")[0].strip()
    return s or "unknown"


# ── 最终哈希 ─────────────────────────────────────────────────────────────────

def content_hash(title: str, company: str, location: str) -> str:
    """返回 16 字符的十六进制哈希, 用于跨平台去重.

    相同公司 + 相同岗位 + 相同地点 → 相同 hash, 无论来自哪个平台.
    """
    nc = normalize_company(company)
    nt = normalize_title(title)
    nl = normalize_location(location)
    key = f"{nc}|{nt}|{nl}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def dedup_key(title: str, company: str, location: str) -> str:
    """返回人类可读的去重 key (用于调试/日志)."""
    return (
        f"{normalize_company(company)}"
        f" | {normalize_title(title)}"
        f" | {normalize_location(location)}"
    )
