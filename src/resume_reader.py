"""简历读取: 把 PDF/DOCX/MD/TXT 解析成纯文本."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


SUPPORTED_EXTS = {".pdf", ".docx", ".md", ".txt"}
PAUSED_SUBDIR = "_paused"


def find_resume(resume_dir: Path) -> Path:
    """在 resume_dir 中找到最新的简历文件."""
    files = list_resumes(resume_dir)
    if not files:
        raise FileNotFoundError(
            f"在 {resume_dir} 中没有找到简历文件 (支持: {sorted(SUPPORTED_EXTS)})"
        )
    return files[0]


def list_resumes(resume_dir: Path) -> list[Path]:
    """列出 resume_dir 下所有"活跃"简历文件 (不含暂停的),按 mtime 降序.

    忽略以下划线开头的内部缓存文件 + 子目录 (_paused/).
    """
    if not resume_dir.exists():
        return []
    files = [
        p for p in resume_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXTS
        and not p.name.startswith("_")
        and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: -p.stat().st_mtime)
    return files


def list_paused_resumes(resume_dir: Path) -> list[Path]:
    """列出 resume_dir/_paused/ 里的简历, mtime 降序."""
    paused_dir = resume_dir / PAUSED_SUBDIR
    if not paused_dir.exists():
        return []
    files = [
        p for p in paused_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    ]
    files.sort(key=lambda p: -p.stat().st_mtime)
    return files


def pause_resume_file(resume_dir: Path, filename: str) -> Path:
    """把一份简历移到 _paused/ 子目录. 返回新路径."""
    src = resume_dir / filename
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"找不到 {filename}")
    if src.suffix.lower() not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的格式 {src.suffix}")
    paused_dir = resume_dir / PAUSED_SUBDIR
    paused_dir.mkdir(exist_ok=True)
    dst = paused_dir / filename
    if dst.exists():
        dst.unlink()  # 覆盖
    src.rename(dst)
    return dst


def unpause_resume_file(resume_dir: Path, filename: str) -> Path:
    """从 _paused/ 还原一份简历回主目录."""
    paused_dir = resume_dir / PAUSED_SUBDIR
    src = paused_dir / filename
    if not src.exists():
        raise FileNotFoundError(f"找不到暂停的 {filename}")
    dst = resume_dir / filename
    if dst.exists():
        raise FileExistsError(f"已有同名简历 {filename}, 先删除或重命名再恢复")
    src.rename(dst)
    return dst


def delete_paused_resume(resume_dir: Path, filename: str) -> None:
    """删除一个暂停状态的简历."""
    paused_dir = resume_dir / PAUSED_SUBDIR
    target = paused_dir / filename
    if target.exists():
        target.unlink()


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
