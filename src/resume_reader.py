"""简历读取: 把 PDF/DOCX/MD/TXT 解析成纯文本."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


SUPPORTED_EXTS = {".pdf", ".docx", ".md", ".txt"}


def find_resume(resume_dir: Path) -> Path:
    """在 resume_dir 中找到最新的简历文件."""
    files = list_resumes(resume_dir)
    if not files:
        raise FileNotFoundError(
            f"在 {resume_dir} 中没有找到简历文件 (支持: {sorted(SUPPORTED_EXTS)})"
        )
    return files[0]


def list_resumes(resume_dir: Path) -> list[Path]:
    """列出 resume_dir 下所有合法简历文件,按 mtime 降序 (最新在前). 找不到目录返回 [].

    忽略以下划线开头的内部缓存文件 (_parsed.txt / _user_description.txt / _profile.json).
    """
    if not resume_dir.exists():
        return []
    files = [
        p for p in resume_dir.iterdir()
        if p.suffix.lower() in SUPPORTED_EXTS
        and not p.name.startswith("_")
        and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: -p.stat().st_mtime)
    return files


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            chunks.append(f"--- Page {i + 1} ---\n{text.strip()}")
    return "\n\n".join(chunks)


def read_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []

    # 段落
    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text.strip())

    # 表格(简历常用表格排版)
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n".join(parts)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_resume(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext == ".docx":
        return read_docx(path)
    if ext in {".md", ".txt"}:
        return read_text(path)
    raise ValueError(f"不支持的简历格式: {ext}")


def parse_and_cache(resume_dir: Path) -> tuple[Path, str]:
    """读取最新简历,把文本缓存为 .cache.txt,返回 (源文件路径, 文本)."""
    src = find_resume(resume_dir)
    text = read_resume(src)
    cache_path = resume_dir / "_parsed.txt"
    cache_path.write_text(text, encoding="utf-8")
    return src, text


def load_cached(resume_dir: Path) -> str:
    """加载已缓存的简历文本;若不存在则现场解析."""
    cache_path = resume_dir / "_parsed.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    _, text = parse_and_cache(resume_dir)
    return text
