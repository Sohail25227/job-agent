"""Turn an uploaded resume into a structured record.

Openings are scored against skills and experience, so the resume has to become
data: a PDF, a DOCX or plain text goes in, and a dict with skills, roles and
bullets comes out. It is stored against the user's row, never on disk.

Extraction is verbatim wherever possible. The LLM here is doing structuring, not
writing: it must not rephrase, summarise or add anything, or the match scores
end up measuring the model's imagination rather than the resume.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from ..config import Config, load_config
from .llm import get_llm

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

STRUCTURE_PROMPT = """You convert a resume's raw text into structured JSON. You are a
parser, not a writer.

RULES:
1. Copy the candidate's wording VERBATIM. Do not rewrite, summarise, shorten or
   improve any sentence.
2. Do not invent anything. If a field is not in the text, use "" or [].
3. Keep every bullet point from every role. Do not merge or drop bullets.
4. Keep all numbers and metrics exactly as written.
5. Dates: copy as written (e.g. "Jan 2026", "2019", "Present").

Return ONLY this JSON shape:
{
  "name": "string",
  "headline": "string (the title line under the name, if any)",
  "contact": {"email": "", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""},
  "summary": "string (the summary/profile paragraph, verbatim)",
  "skills": {"Category Name": ["skill", "..."]},
  "experience": [
    {"company": "", "title": "", "location": "", "start": "", "end": "",
     "team": "", "tech": ["..."], "bullets": ["verbatim bullet", "..."]}
  ],
  "education": [{"degree": "", "institution": "", "location": "", "start": "", "end": "", "detail": ""}],
  "certifications": [{"name": "", "issuer": "", "year": ""}],
  "projects": [{"name": "", "tech": ["..."], "detail": ""}]
}"""

SECTION_HEADERS = re.compile(
    r"^\s*(summary|profile|objective|technical skills|skills|work experience|experience"
    r"|employment|professional experience|education|projects|certifications?"
    r"|achievements|awards)\s*:?\s*$",
    re.I,
)


@dataclass(slots=True)
class IngestResult:
    resume: dict
    engine: str
    source_file: str
    warnings: list[str] = field(default_factory=list)

    @property
    def summary_line(self) -> str:
        roles = len(self.resume.get("experience") or [])
        bullets = sum(len(r.get("bullets") or []) for r in self.resume.get("experience") or [])
        skills = sum(len(v or []) for v in (self.resume.get("skills") or {}).values())
        return f"{roles} role(s), {bullets} bullet(s), {skills} skill(s)"


# --------------------------------------------------------------------------
# text extraction
# --------------------------------------------------------------------------

def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_bytes(filename: str, data: bytes) -> str:
    """Read a resume without ever storing it.

    The upload is a private document and the server it runs on may have an
    ephemeral disk, so it is parsed from memory and the bytes are dropped; only
    the structured result is kept, against the user's row.
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif suffix == ".docx":
        from docx import Document

        document = Document(io.BytesIO(data))
        blocks = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                blocks.extend(cell.text for cell in row.cells)
        text = "\n".join(blocks)
    elif suffix in {".txt", ".md"}:
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported resume format {suffix!r}. Use PDF, DOCX, TXT or MD.")
    return _tidy(text)


def extract_text(path: Path) -> str:
    return extract_text_from_bytes(path.name, path.read_bytes())


# --------------------------------------------------------------------------
# structuring
# --------------------------------------------------------------------------

def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalise_structure(raw: dict) -> dict:
    """Coerce whatever the model returned into the exact schema we rely on."""
    contact = raw.get("contact") or {}
    resume: dict = {
        "name": _clean(raw.get("name")),
        "headline": _clean(raw.get("headline")),
        "contact": {
            key: _clean(contact.get(key))
            for key in ("email", "phone", "location", "linkedin", "github", "website")
        },
        "summary": _clean(raw.get("summary")),
        "skills": {},
        "experience": [],
        "education": [],
        "certifications": [],
        "projects": [],
    }

    skills = raw.get("skills")
    if isinstance(skills, dict):
        for category, values in skills.items():
            items = [_clean(v) for v in (values or []) if _clean(v)]
            if items:
                resume["skills"][_clean(category) or "Skills"] = items
    elif isinstance(skills, list):
        items = [_clean(v) for v in skills if _clean(v)]
        if items:
            resume["skills"]["Skills"] = items

    for role in raw.get("experience") or []:
        if not isinstance(role, dict):
            continue
        bullets = [_clean(b) for b in role.get("bullets") or [] if _clean(b)]
        if not (role.get("company") or role.get("title")):
            continue
        resume["experience"].append(
            {
                "company": _clean(role.get("company")),
                "title": _clean(role.get("title")),
                "location": _clean(role.get("location")),
                "start": _clean(role.get("start")),
                "end": _clean(role.get("end")),
                "team": _clean(role.get("team")),
                "tech": [_clean(t) for t in role.get("tech") or [] if _clean(t)],
                "bullets": bullets,
            }
        )

    for entry in raw.get("education") or []:
        if isinstance(entry, dict) and (entry.get("degree") or entry.get("institution")):
            resume["education"].append(
                {
                    "degree": _clean(entry.get("degree")),
                    "institution": _clean(entry.get("institution")),
                    "location": _clean(entry.get("location")),
                    "start": _clean(entry.get("start")),
                    "end": _clean(entry.get("end")),
                    "detail": _clean(entry.get("detail")),
                }
            )

    for entry in raw.get("certifications") or []:
        if isinstance(entry, dict) and entry.get("name"):
            resume["certifications"].append(
                {
                    "name": _clean(entry.get("name")),
                    "issuer": _clean(entry.get("issuer")),
                    "year": _clean(entry.get("year")),
                }
            )
        elif isinstance(entry, str) and _clean(entry):
            resume["certifications"].append({"name": _clean(entry), "issuer": "", "year": ""})

    for entry in raw.get("projects") or []:
        if isinstance(entry, dict) and entry.get("name"):
            resume["projects"].append(
                {
                    "name": _clean(entry.get("name")),
                    "tech": [_clean(t) for t in entry.get("tech") or [] if _clean(t)],
                    "detail": _clean(entry.get("detail")),
                }
            )

    return resume


def _heuristic_structure(text: str) -> tuple[dict, list[str]]:
    """No-LLM fallback: split on section headers and keep the text intact."""
    warnings = [
        "Parsed without an LLM, so sections are approximate. "
        "Open config/resume_base.yaml and check the roles and bullets."
    ]
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]

    email = next((m.group(0) for line in non_empty
                  if (m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", line))), "")
    phone = next((m.group(0) for line in non_empty
                  if (m := re.search(r"(?:\+?\d[\d\s().-]{8,}\d)", line))), "")
    linkedin = next((m.group(0) for line in non_empty
                     if (m := re.search(r"linkedin\.com/\S+", line, re.I))), "")
    github = next((m.group(0) for line in non_empty
                   if (m := re.search(r"github\.com/\S+", line, re.I))), "")

    sections: dict[str, list[str]] = {}
    current = "header"
    for line in lines:
        if not line:
            continue
        if SECTION_HEADERS.match(line):
            current = line.strip(": ").lower()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    header = sections.get("header") or non_empty[:3]
    summary_block = next(
        (v for k, v in sections.items() if k in {"summary", "profile", "objective"}), []
    )
    skills_block = next(
        (v for k, v in sections.items() if "skill" in k), []
    )
    experience_block = next(
        (v for k, v in sections.items()
         if k in {"experience", "work experience", "employment", "professional experience"}), []
    )

    resume = {
        "name": _clean(header[0]) if header else "",
        "headline": _clean(header[1]) if len(header) > 1 else "",
        "contact": {
            "email": email, "phone": phone, "location": "",
            "linkedin": linkedin, "github": github, "website": "",
        },
        "summary": _clean(" ".join(summary_block)),
        "skills": {"Skills": [s.strip() for s in re.split(r"[,•|]", " ".join(skills_block))
                              if 1 < len(s.strip()) < 40][:60]},
        "experience": (
            [{
                "company": "REVIEW ME", "title": "REVIEW ME", "location": "",
                "start": "", "end": "", "team": "", "tech": [],
                "bullets": [_clean(line) for line in experience_block if len(line) > 30][:20],
            }]
            if experience_block else []
        ),
        "education": [],
        "certifications": [],
        "projects": [],
    }
    return resume, warnings


def structure_resume(text: str, config: Config | None = None) -> IngestResult:
    config = config or load_config()
    warnings: list[str] = []

    llm = get_llm(config)
    if llm is not None:
        try:
            raw = llm.complete_json(STRUCTURE_PROMPT, text[:24000])
            resume = _normalise_structure(raw)
            if resume.get("experience") and resume.get("name"):
                return IngestResult(resume, llm.label, "", warnings)
            warnings.append("LLM output was missing the name or work history; used a text parse instead.")
        except Exception as exc:
            log.warning("LLM resume parsing failed: %s", exc)
            warnings.append(f"LLM parsing failed ({str(exc)[:120]}); used a text parse instead.")

    resume, fallback_warnings = _heuristic_structure(text)
    return IngestResult(resume, "text parser", "", warnings + fallback_warnings)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def save_base_resume(resume: dict, config: Config | None = None) -> Path:
    """Write resume_base.yaml, keeping a timestamped backup of the previous one."""
    config = config or load_config()
    target = config.resume.base_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak.yaml")
        shutil.copy(target, backup)
        log.info("previous master resume backed up to %s", backup.name)

    header = (
        "# Master resume, generated from an uploaded file.\n"
        "# Every job found is scored against the skills and experience listed here,\n"
        "# so correct anything the parser got wrong.\n"
    )
    target.write_text(
        header + yaml.safe_dump(resume, sort_keys=False, allow_unicode=True, width=100)
    )
    return target


TOO_LITTLE_TEXT = (
    "Could not read enough text from that file. If it is a scanned or "
    "image-based PDF, export a text PDF from Word/Docs and try again."
)


def parse_upload(filename: str, data: bytes, config: Config | None = None) -> IngestResult:
    """Structure an uploaded resume straight from memory."""
    config = config or load_config()
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"Unsupported format {Path(filename).suffix!r}. Use PDF, DOCX, TXT or MD."
        )

    text = extract_text_from_bytes(filename, data)
    if len(text) < 200:
        raise ValueError(TOO_LITTLE_TEXT)

    result = structure_resume(text, config)
    result.source_file = Path(filename).name
    return result


def ingest_file(path: Path, config: Config | None = None) -> IngestResult:
    """Parse a resume from disk and write ``resume_base.yaml``. CLI only."""
    result = parse_upload(path.name, path.read_bytes(), config)
    save_base_resume(result.resume, config)
    return result


def store_upload(filename: str, data: bytes, config: Config | None = None) -> Path:
    config = config or load_config()
    uploads = config.web.upload_path
    uploads.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name) or "resume.pdf"
    target = uploads / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
    target.write_bytes(data)
    return target
