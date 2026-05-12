"""Resume profile analyzer (three-round pipeline version).

Three-round multi-turn conversation strategy:
  Round 1 — Extraction (profile_extract.md):
      resume + materials → structured skill profile + ATS keyword pool
  Round 2 — Evaluation (profile_perspectives.md):
      extraction results → HR perspective / HM perspective / strategist perspective → candidate positioning analysis
  Round 3 — Output (profile_analyzer.md):
      full context + user requirements → Top-10 positions (with aliases/broader_terms)

Each round's output is passed as part of the next round's message history,
allowing the model to make decisions in complete context rather than starting from scratch each time.

Final output is saved to data/resume/_profile.json,
then collect phase automatically reads top_10_positions' title + aliases + broader_terms as search keywords.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from .agent import load_prompt, make_client, render
from .config import Config
from .db import Profile, session_scope
from .resume_reader import load_cached


_COMPANY_LIST_SCHEMA = {
    "type": "array",
    "minItems": 3,
    "maxItems": 8,
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "why_fit": {"type": "string", "description": "Why the candidate is a fit (English, <=50 chars)"},
            "hiring_signal": {"type": "string", "description": "Current expansion/hiring signal (English, <=50 chars)"},
            "example_roles": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
                "description": "Possible specific position title examples (English)",
            },
            "careers_url": {"type": "string", "description": "Careers/hiring page URL (optional)"},
        },
        "required": ["name", "why_fit", "hiring_signal", "example_roles"],
    },
}


PROFILE_TOOL: dict[str, Any] = {
    "name": "submit_profile_analysis",
    "description": "Submit Top-10 optimal job positions analysis for the candidate",
    "input_schema": {
        "type": "object",
        "properties": {
            "top_10_positions": {
                "type": "array",
                "minItems": 10,
                "maxItems": 10,
                "description": "Top 10 positions to apply for, sorted by composite score descending. Will use aliases + broader_terms for fuzzy expansion later.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Standard LinkedIn title (English, no made-up words)",
                        },
                        "direction": {
                            "type": "string",
                            "enum": [
                                "engineering",
                                "research-engineering",
                                "internship",
                            ],
                            "description": (
                                "engineering (SWE/MLE/Backend/Platform/Infra full-time) | "
                                "research-engineering (Anthropic/DeepMind-like industrial lab full-time) | "
                                "internship (internship positions, title must include Intern/Co-op). "
                                "Industry only, no academic."
                            ),
                        },
                        "scores": {
                            "type": "object",
                            "properties": {
                                "market_demand": {
                                    "type": "integer", "minimum": 0, "maximum": 10,
                                    "description": "Current market hiring volume",
                                },
                                "competition": {
                                    "type": "integer", "minimum": 0, "maximum": 10,
                                    "description": "Competition intensity, lower is better",
                                },
                                "user_advantage": {
                                    "type": "integer", "minimum": 0, "maximum": 10,
                                    "description": "Candidate match depth",
                                },
                                "composite": {
                                    "type": "integer", "minimum": 0, "maximum": 100,
                                    "description": "= market_demand * (10-competition) * user_advantage / 10",
                                },
                            },
                            "required": ["market_demand", "competition", "user_advantage", "composite"],
                        },
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 5,
                            "description": "2-5 real market synonym/variant titles (English)",
                        },
                        "broader_terms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 0,
                            "maxItems": 3,
                            "description": "0-3 broader titles that might hide this direction (English, e.g., some AI companies use 'Software Engineer' but actually mean ML roles)",
                        },
                        "why_this_position": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 4,
                            "maxItems": 6,
                            "description": (
                                "4-6 bullets (English), first 2 starting with '[HR]': "
                                "HR perspective—keyword coverage/education threshold/title match; "
                                "next 2-4 starting with '[HM]': "
                                "HM perspective—cite specific projects/numbers/papers/open source contributions from resume or materials, "
                                "explain differentiated value candidate brings to team"
                            ),
                        },
                        "market_evidence": {
                            "type": "string",
                            "description": "Why market is hot (<=50 chars, English)",
                        },
                        "linkedin_search_url": {
                            "type": "string",
                            "description": "Direct clickable LinkedIn search URL",
                        },
                    },
                    "required": [
                        "title", "direction", "scores", "aliases", "broader_terms",
                        "why_this_position", "market_evidence", "linkedin_search_url",
                    ],
                },
            },
            "target_locations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
                "description": "Recommended target locations (LinkedIn recognizable)",
            },
            "recommended_companies": {
                "type": "object",
                "description": "Target company list grouped by 5 regions (background match + active expansion)",
                "properties": {
                    "north_america": _COMPANY_LIST_SCHEMA,
                    "hong_kong": _COMPANY_LIST_SCHEMA,
                    "singapore": _COMPANY_LIST_SCHEMA,
                    "japan": _COMPANY_LIST_SCHEMA,
                    "europe": _COMPANY_LIST_SCHEMA,
                },
                "required": ["north_america", "hong_kong", "singapore", "japan", "europe"],
            },
            "summary": {
                "type": "string",
                "description": "Candidate core positioning + application strategy (<=80 chars, English)",
            },
        },
        "required": ["top_10_positions", "target_locations", "recommended_companies", "summary"],
    },
}


# ── Round 1 tool: Skill extraction ──────────────────────────────────────────────────

EXTRACT_TOOL: dict[str, Any] = {
    "name": "submit_skill_extraction",
    "description": "Submit structured skill profile extracted from resume and materials library",
    "input_schema": {
        "type": "object",
        "properties": {
            "technical_skills": {
                "type": "array",
                "description": "Complete list of technical skills",
                "items": {
                    "type": "object",
                    "properties": {
                        "skill":    {"type": "string"},
                        "level":    {"type": "string",
                                     "enum": ["exposure", "proficient", "deep_project", "published_or_open_source"],
                                     "description": "exposure / proficient / deep project experience / published or open source contribution"},
                        "source":   {"type": "string",
                                     "enum": ["resume", "materials", "both"]},
                        "evidence": {"type": "string",
                                     "description": "One sentence of evidence, example: 'Implemented LLM inference engine with PyTorch, 3x throughput improvement'"},
                    },
                    "required": ["skill", "level", "source"],
                },
            },
            "key_projects": {
                "type": "array",
                "description": "Most important 3-6 projects/experiences",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":       {"type": "string"},
                        "scale":      {"type": "string", "description": "solo / small team / large system"},
                        "impact":     {"type": "string", "description": "quantified impact, with numbers if possible"},
                        "tech_stack": {"type": "array", "items": {"type": "string"}},
                        "source":     {"type": "string", "enum": ["resume", "materials", "both"]},
                    },
                    "required": ["name", "impact", "tech_stack"],
                },
            },
            "materials_highlights": {
                "type": "array",
                "description": "Deep content shown in materials library but not fully represented in resume (papers/articles/open source etc)",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":       {"type": "string"},
                        "type":        {"type": "string",
                                        "enum": ["paper", "article", "open_source", "project_doc", "other"]},
                        "tech_depth":  {"type": "string", "description": "One sentence description of technical direction and depth"},
                        "signal":      {"type": "string", "description": "External validation signal: publication venue/GitHub stars/citation count etc"},
                    },
                    "required": ["title", "type", "tech_depth"],
                },
            },
            "ats_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "ATS keyword pool extracted from all materials, 20-50 items, for HR perspective evaluation and JD matching",
            },
            "experience_years": {
                "type": "number",
                "description": "Total years of experience (excluding ongoing education)",
            },
            "education_level": {
                "type": "string",
                "enum": ["bachelor", "master", "phd", "other"],
            },
        },
        "required": ["technical_skills", "key_projects", "ats_keywords",
                     "experience_years", "education_level"],
    },
}


# ── Round 2 tool: Three-perspective evaluation ────────────────────────────────────────────────

PERSPECTIVES_TOOL: dict[str, Any] = {
    "name": "submit_perspectives",
    "description": "Submit comprehensive three-perspective evaluation (HR / HM / strategist) of candidate",
    "input_schema": {
        "type": "object",
        "properties": {
            "hr_view": {
                "type": "object",
                "properties": {
                    "strong_match_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Directions HR would directly approve (strong keyword coverage + threshold met)",
                    },
                    "conditional_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Directions HR would hesitate on (partially meets requirements, needs supplementary materials or lower-level position)",
                    },
                    "weak_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Directions HR would directly reject",
                    },
                    "keyword_coverage_note": {
                        "type": "string",
                        "description": "Brief explanation of ATS keyword coverage",
                    },
                },
                "required": ["strong_match_directions", "conditional_directions", "keyword_coverage_note"],
            },
            "hm_view": {
                "type": "object",
                "properties": {
                    "proven_delivery": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Technical directions with clear delivery proof (cite specific projects/numbers/papers)",
                    },
                    "differentiators": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Qualities HM would see as giving candidate differentiated value (what most peer competitors lack)",
                    },
                    "main_concerns": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HM's most likely concerns (e.g., short experience in certain direction / breadth over depth)",
                    },
                    "high_value_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Directions where HM sees candidate bringing unique value",
                    },
                },
                "required": ["proven_delivery", "differentiators", "high_value_directions"],
            },
            "strategist_view": {
                "type": "object",
                "properties": {
                    "high_roi_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "High-ROI directions with strong demand + candidate has competitive advantage",
                    },
                    "avoid_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Directions too competitive or where candidate advantage unclear, recommended to avoid",
                    },
                    "key_positioning": {
                        "type": "string",
                        "description": "One sentence summary of market positioning candidate should focus on (English, <=50 chars)",
                    },
                },
                "required": ["high_roi_directions", "key_positioning"],
            },
            "combined_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Combined from three perspectives: keywords to emphasize in resume/cover letter, 15-30 items, sorted by importance descending",
            },
        },
        "required": ["hr_view", "hm_view", "strategist_view", "combined_keywords"],
    },
}


@dataclass
class PositionScores:
    market_demand: int = 0
    competition: int = 0
    user_advantage: int = 0
    composite: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "PositionScores":
        return cls(
            market_demand=int(d.get("market_demand", 0)),
            competition=int(d.get("competition", 0)),
            user_advantage=int(d.get("user_advantage", 0)),
            composite=int(d.get("composite", 0)),
        )


@dataclass
class Position:
    title: str
    direction: str
    scores: PositionScores
    aliases: list[str] = field(default_factory=list)
    broader_terms: list[str] = field(default_factory=list)
    why_this_position: list[str] = field(default_factory=list)
    market_evidence: str = ""
    linkedin_search_url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            title=str(d.get("title", "")).strip(),
            direction=str(d.get("direction", "engineering")),
            scores=PositionScores.from_dict(d.get("scores") or {}),
            aliases=list(d.get("aliases") or []),
            broader_terms=list(d.get("broader_terms") or []),
            why_this_position=list(d.get("why_this_position") or []),
            market_evidence=str(d.get("market_evidence", "")),
            linkedin_search_url=str(d.get("linkedin_search_url", "")),
        )

    def all_search_terms(self, include_broader: bool = False) -> list[str]:
        """primary + aliases [+ broader_terms]. Keep order, remove empty strings."""
        out = [self.title] + list(self.aliases)
        if include_broader:
            out += list(self.broader_terms)
        return [t.strip() for t in out if t and t.strip()]


@dataclass
class Company:
    name: str
    why_fit: str = ""
    hiring_signal: str = ""
    example_roles: list[str] = field(default_factory=list)
    careers_url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Company":
        return cls(
            name=str(d.get("name", "")).strip(),
            why_fit=str(d.get("why_fit", "")),
            hiring_signal=str(d.get("hiring_signal", "")),
            example_roles=list(d.get("example_roles") or []),
            careers_url=str(d.get("careers_url", "")),
        )


@dataclass
class RegionalCompanies:
    north_america: list[Company] = field(default_factory=list)
    hong_kong: list[Company] = field(default_factory=list)
    singapore: list[Company] = field(default_factory=list)
    japan: list[Company] = field(default_factory=list)
    europe: list[Company] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "RegionalCompanies":
        if not d:
            return cls()
        return cls(
            north_america=[Company.from_dict(c) for c in (d.get("north_america") or [])],
            hong_kong=[Company.from_dict(c) for c in (d.get("hong_kong") or [])],
            singapore=[Company.from_dict(c) for c in (d.get("singapore") or [])],
            japan=[Company.from_dict(c) for c in (d.get("japan") or [])],
            europe=[Company.from_dict(c) for c in (d.get("europe") or [])],
        )

    def to_dict(self) -> dict:
        return {
            "north_america": [asdict(c) for c in self.north_america],
            "hong_kong": [asdict(c) for c in self.hong_kong],
            "singapore": [asdict(c) for c in self.singapore],
            "japan": [asdict(c) for c in self.japan],
            "europe": [asdict(c) for c in self.europe],
        }

    def regions(self) -> list[tuple[str, list[Company]]]:
        """Return (region_label, companies) in display order."""
        return [
            ("North America (US/Canada)", self.north_america),
            ("Hong Kong", self.hong_kong),
            ("Singapore", self.singapore),
            ("Japan", self.japan),
            ("Europe", self.europe),
        ]


@dataclass
class ProfileAnalysis:
    top_10_positions: list[Position]
    target_locations: list[str]
    summary: str
    recommended_companies: RegionalCompanies = field(default_factory=RegionalCompanies)

    @classmethod
    def from_tool_input(cls, data: dict) -> "ProfileAnalysis":
        # New schema uses top_10_positions; for backward compatibility with old schema using top_5_positions
        raw = data.get("top_10_positions") or data.get("top_5_positions") or []
        positions = [Position.from_dict(p) for p in raw]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_10_positions=positions,
            target_locations=list(data.get("target_locations") or []),
            summary=str(data.get("summary", "")),
            recommended_companies=RegionalCompanies.from_dict(data.get("recommended_companies") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "top_10_positions": [
                {
                    "title": p.title,
                    "direction": p.direction,
                    "scores": asdict(p.scores),
                    "aliases": p.aliases,
                    "broader_terms": p.broader_terms,
                    "why_this_position": p.why_this_position,
                    "market_evidence": p.market_evidence,
                    "linkedin_search_url": p.linkedin_search_url,
                }
                for p in self.top_10_positions
            ],
            "target_locations": self.target_locations,
            "summary": self.summary,
            "recommended_companies": self.recommended_companies.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileAnalysis":
        # Backward compatible with old JSON files (top_5_positions) and new (top_10_positions)
        raw = data.get("top_10_positions") or data.get("top_5_positions") or []
        positions = [Position.from_dict(p) for p in raw]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_10_positions=positions,
            target_locations=data.get("target_locations", []),
            summary=data.get("summary", ""),
            recommended_companies=RegionalCompanies.from_dict(data.get("recommended_companies") or {}),
        )

    def search_titles(
        self, *, include_aliases: bool = True, include_broader: bool = True, limit: int = 40
    ) -> list[str]:
        """Aggregate search terms from 10 positions. Fuzzy expansion enabled by default — primary + aliases + broader.

        Flow: first collect 10 primary titles as baseline, then expand aliases (synonyms), then expand broader_terms
        (hidden opportunities). Case-insensitive deduplication, maintain order.
        """
        seen: set[str] = set()
        out: list[str] = []
        # First collect all primary titles (ensure no Top-10 is missed)
        for p in self.top_10_positions:
            t = (p.title or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        # Then fuzzy expansion: aliases then broader_terms
        for tier in ("aliases", "broader"):
            for p in self.top_10_positions:
                terms: list[str] = []
                if tier == "aliases" and include_aliases:
                    terms = list(p.aliases)
                elif tier == "broader" and include_broader:
                    terms = list(p.broader_terms)
                for t in terms:
                    t_clean = (t or "").strip()
                    if t_clean and t_clean.lower() not in seen:
                        seen.add(t_clean.lower())
                        out.append(t_clean)
                    if len(out) >= limit:
                        return out
        return out

    def primary_titles(self) -> list[str]:
        """Get only 10 primary titles."""
        return [p.title for p in self.top_10_positions if p.title]

    # Backward compatibility: old code used unique_search_titles / top_5_positions
    def unique_search_titles(self, limit: int = 40) -> list[str]:
        return self.search_titles(include_aliases=True, include_broader=True, limit=limit)

    @property
    def top_5_positions(self) -> list[Position]:
        """Legacy interface compatibility: return first 5 (already sorted by composite)."""
        return self.top_10_positions[:5]


def _profile_path(config: Config) -> Path:
    return config.path("resume_dir") / "_profile.json"


def _user_desc_path(config: Config) -> Path:
    return config.path("resume_dir") / "_user_description.txt"


def save_user_description(config: Config, text: str) -> Path:
    path = _user_desc_path(config)
    path.write_text(text.strip(), encoding="utf-8")
    return path


def load_user_description(config: Config) -> str | None:
    path = _user_desc_path(config)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def save_profile(config: Config, profile: ProfileAnalysis) -> Path:
    """Save profile to JSON file (for code reading)."""
    path = _profile_path(config)
    path.write_text(
        json.dumps(profile.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def save_profile_snapshot(
    config: Config,
    profile: ProfileAnalysis,
    user_description: str,
    resume_filename: str | None = None,
) -> int:
    """Save profile to DB (profiles table) as new row, mark as current. Return profile_id.

    Works together with save_profile: JSON is the "currently active profile", DB is historical snapshot.
    """
    label = (user_description.strip().splitlines()[0] if user_description else "")[:60] or "Profile"
    profile_json = json.dumps(profile.to_dict(), ensure_ascii=False)
    db_path = config.path("db_path")

    with session_scope(db_path) as session:
        # Deactivate old current profile
        session.execute(update(Profile).where(Profile.is_current).values(is_current=False))
        new_row = Profile(
            label=label,
            user_description=user_description,
            resume_filename=resume_filename,
            profile_json=profile_json,
            is_current=True,
        )
        session.add(new_row)
        session.commit()
        return new_row.id


def list_profile_snapshots(config: Config) -> list[Profile]:
    """Return all historical profile snapshots, newest → oldest."""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        rows = list(session.scalars(
            select(Profile).order_by(Profile.created_at.desc())
        ).all())
        for r in rows:
            session.expunge(r)
        return rows


def get_current_profile_id(config: Config) -> int | None:
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        row = session.scalar(select(Profile).where(Profile.is_current))
        return row.id if row else None


def activate_profile_snapshot(config: Config, profile_id: int) -> Profile:
    """Set a historical profile as current, write its profile_json back to JSON file + sync user_description.

    Return the activated Profile object.
    """
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        row = session.get(Profile, profile_id)
        if not row:
            raise ValueError(f"Profile #{profile_id} does not exist")

        session.execute(update(Profile).where(Profile.is_current).values(is_current=False))
        row.is_current = True
        session.add(row)
        session.commit()

        # Sync JSON + user description file
        _profile_path(config).write_text(row.profile_json, encoding="utf-8")
        save_user_description(config, row.user_description)
        session.expunge(row)
        return row


def load_profile(config: Config) -> ProfileAnalysis | None:
    path = _profile_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Backward compatibility with old format (top_directions): if old format, return None to force regeneration
        if (
            "top_directions" in data
            and "top_5_positions" not in data
            and "top_10_positions" not in data
        ):
            print("[profile] Old _profile.json format incompatible, please run `analyze-profile --force` to regenerate")
            return None
        return ProfileAnalysis.from_dict(data)
    except Exception as e:
        print(f"[profile] Failed to load cache: {e}")
        return None


def analyze_profile(
    config: Config,
    user_description: str | None = None,
    job_types: list[str] | None = None,
) -> ProfileAnalysis:
    """Three-round pipeline to analyze candidate profile.

    Round 1: resume + materials → structured skill extraction
    Round 2: extraction results → HR / HM / strategist three-perspective evaluation
    Round 3: full context + user requirements + job_types → Top-10 positions

    job_types: ["Full-time"] / ["Internship"] / ["Full-time","Internship"]
    Different types generate differently positioned Top-10 (internship vs full-time employee).
    """
    from .resume_reader import read_materials

    resume_text = load_cached(config.path("resume_dir"))
    if user_description is None:
        user_description = load_user_description(config)
    materials_text = read_materials(config.path("materials_dir"))
    materials_str = materials_text or "(Candidate has not uploaded any materials library materials)"

    # Build job_types context description, inject into Round 3
    if not job_types:
        job_types = config.preferences.get("job_types") or ["Full-time"]
    is_intern_only  = job_types == ["Internship"] or job_types == ["intern"]
    is_ft_only      = "Internship" not in job_types and "intern" not in [j.lower() for j in job_types]
    if is_intern_only:
        job_type_instruction = (
            "**This analysis is specifically for internship positions.**\n"
            "All Top-10 directions must be internship positions with titles explicitly containing 'Intern', 'Internship', or 'Co-op'.\n"
            "Evaluation criteria should focus on student or recent graduate perspective, without requiring years of work experience.\n"
            "Search terms (aliases/broader_terms) should also include internship-related variants like 'Software Engineer Intern', 'ML Research Intern', etc."
        )
    elif is_ft_only:
        job_type_instruction = (
            "**This analysis is specifically for full-time positions.**\n"
            "All Top-10 directions target full-time positions, do not include any internship (Intern/Internship/Co-op) titles.\n"
            "aliases and broader_terms must not contain internship-related vocabulary."
        )
    else:
        # Multiple types: profile analysis generates generic Top-10, collect phase runs each type separately
        job_type_instruction = (
            f"**This analysis covers: {', '.join(job_types)} (collect phase will run separately for each).**\n"
            "Top-10 should use generic position directions without Intern suffix, collect phase will apply Full-time and Internship filters separately.\n"
            "aliases and broader_terms should also use generic phrasing."
        )

    role = "profile_analyzer"
    if not config.raw.get("model", {}).get(role):
        role = "matcher"
    client, model_name, provider = make_client(config, role)

    print(f"[profile] Materials: {len(materials_text) if materials_text else 0} chars — sending full content (1M context)")

    # ── Round 1: Skill extraction ───────────────────────────────────────────────────
    from .agent import _convert_tool_to_openai

    print("[profile] Round 1 — Extracting skill profile and keywords...")
    r1_prompt = render(
        load_prompt("profile_extract"),
        resume=resume_text,
        materials=materials_str,
    )

    r1_tool_result = None
    _r1_tool_id = None

    if provider == "deepseek":
        # OpenAI format
        tools = [_convert_tool_to_openai(EXTRACT_TOOL)]
        with client.chat.completions.stream(
            model=model_name,
            max_tokens=32000,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_skill_extraction"}},
            messages=[{"role": "user", "content": r1_prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            r1_resp = stream.get_final_completion()
        if r1_resp.choices[0].message.tool_calls:
            tool_call = r1_resp.choices[0].message.tool_calls[0]
            r1_tool_result = json.loads(tool_call.function.arguments)
            _r1_tool_id = tool_call.id
    else:
        # Anthropic format
        with client.messages.stream(
            model=model_name,
            max_tokens=32000,
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "submit_skill_extraction"},
            messages=[{"role": "user", "content": r1_prompt}],
        ) as _s:
            r1_resp = _s.get_final_message()
        for block in r1_resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_skill_extraction":
                r1_tool_result = block.input
                _r1_tool_id = block.id
                break

    if r1_tool_result is None:
        raise RuntimeError("[profile] Round 1 failed: model did not return submit_skill_extraction")
    if not _r1_tool_id:
        raise RuntimeError("[profile] Round 1 response missing tool_use block id")

    r1_summary = json.dumps(r1_tool_result, ensure_ascii=False, indent=2)
    print(f"[profile] Round 1 complete: extracted {len(r1_tool_result.get('technical_skills', []))} skills, "
          f"{len(r1_tool_result.get('ats_keywords', []))} keywords")

    # ── Round 2: Three-perspective evaluation (carrying full Round 1 conversation history) ───────────────────
    print("[profile] Round 2 — HR / HM / strategist three-perspective evaluation...")
    r2_prompt = load_prompt("profile_perspectives")

    r2_tool_result = None
    _r2_tool_id = None

    if provider == "deepseek":
        # OpenAI format: build message history with tool result in OpenAI format
        messages: list[dict] = [
            {"role": "user", "content": r1_prompt},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": _r1_tool_id,
                        "type": "function",
                        "function": {
                            "name": "submit_skill_extraction",
                            "arguments": r1_summary,
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": _r1_tool_id,
                "content": r1_summary,
            },
            {"role": "assistant", "content": "Skill extraction complete. Starting three-perspective analysis."},
            {"role": "user", "content": r2_prompt},
        ]
        tools = [_convert_tool_to_openai(PERSPECTIVES_TOOL)]
        with client.chat.completions.stream(
            model=model_name,
            max_tokens=32000,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_perspectives"}},
            messages=messages,
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            r2_resp = stream.get_final_completion()
        if r2_resp.choices[0].message.tool_calls:
            tool_call = r2_resp.choices[0].message.tool_calls[0]
            r2_tool_result = json.loads(tool_call.function.arguments)
            _r2_tool_id = tool_call.id
    else:
        # Anthropic format: use native message history structure
        messages: list[dict] = [
            {"role": "user", "content": r1_prompt},
            {"role": "assistant", "content": r1_resp.content},
            {"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": _r1_tool_id,
                    "content": r1_summary,
                }
            ]},
            {"role": "assistant", "content": "Skill extraction complete. Starting three-perspective analysis."},
            {"role": "user", "content": r2_prompt},
        ]
        with client.messages.stream(
            model=model_name,
            max_tokens=32000,
            tools=[PERSPECTIVES_TOOL],
            tool_choice={"type": "tool", "name": "submit_perspectives"},
            messages=messages,
        ) as _s:
            r2_resp = _s.get_final_message()
        for block in r2_resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_perspectives":
                r2_tool_result = block.input
                _r2_tool_id = block.id
                break

    if r2_tool_result is None:
        raise RuntimeError("[profile] Round 2 failed: model did not return submit_perspectives")
    if not _r2_tool_id:
        raise RuntimeError("[profile] Round 2 response missing tool_use block id")

    r2_summary = json.dumps(r2_tool_result, ensure_ascii=False, indent=2)
    combined_kw = r2_tool_result.get("combined_keywords", [])
    print(f"[profile] Round 2 complete: {len(combined_kw)} core keywords, "
          f"strong match directions: {r2_tool_result.get('hr_view', {}).get('strong_match_directions', [])}")

    # ── Round 3: Top-10 output (with full conversation history) ─────────────────────────────
    print("[profile] Round 3 — Generating Top-10 optimal submission directions...")
    r3_prompt = render(
        load_prompt("profile_analyzer"),
        user_description=user_description or "(Candidate did not provide a self-description, inferred based on resume and default preferences only)",
        preferences=json.dumps(config.preferences, ensure_ascii=False, indent=2),
        job_type_instruction=job_type_instruction,
    )

    if provider == "deepseek":
        # OpenAI format: build full multi-round history with tool calls
        messages_r3: list[dict] = [
            {"role": "user", "content": r1_prompt},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": _r1_tool_id,
                        "type": "function",
                        "function": {
                            "name": "submit_skill_extraction",
                            "arguments": r1_summary,
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": _r1_tool_id,
                "content": r1_summary,
            },
            {"role": "assistant", "content": "Skill extraction complete. Starting three-perspective analysis."},
            {"role": "user", "content": r2_prompt},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": _r2_tool_id,
                        "type": "function",
                        "function": {
                            "name": "submit_perspectives",
                            "arguments": r2_summary,
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": _r2_tool_id,
                "content": r2_summary,
            },
            {"role": "assistant", "content": "Completed three-perspective evaluation. Starting to generate Top-10 submission directions."},
            {"role": "user", "content": r3_prompt},
        ]
        tools = [_convert_tool_to_openai(PROFILE_TOOL)]
        with client.chat.completions.stream(
            model=model_name,
            max_tokens=32000,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_profile_analysis"}},
            messages=messages_r3,
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            r3_resp = stream.get_final_completion()
        if r3_resp.choices[0].message.tool_calls:
            tool_call = r3_resp.choices[0].message.tool_calls[0]
            print("[profile] Round 3 complete: Top-10 generated successfully")
            return ProfileAnalysis.from_tool_input(json.loads(tool_call.function.arguments))
        raise RuntimeError("[profile] Round 3 failed: model did not return submit_profile_analysis")
    else:
        # Anthropic format: use native message history structure
        messages_r3: list[dict] = [
            *messages,
            {"role": "assistant", "content": r2_resp.content},
            {"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": _r2_tool_id,
                    "content": r2_summary,
                }
            ]},
            {"role": "assistant", "content": "Completed three-perspective evaluation. Starting to generate Top-10 submission directions."},
            {"role": "user", "content": r3_prompt},
        ]
        with client.messages.stream(
            model=model_name,
            max_tokens=32000,
            tools=[PROFILE_TOOL],
            tool_choice={"type": "tool", "name": "submit_profile_analysis"},
            messages=messages_r3,
        ) as _s:
            r3_resp = _s.get_final_message()
        for block in r3_resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_profile_analysis":
                print("[profile] Round 3 complete: Top-10 generated successfully")
                return ProfileAnalysis.from_tool_input(block.input)
        raise RuntimeError("[profile] Round 3 failed: model did not return submit_profile_analysis")
