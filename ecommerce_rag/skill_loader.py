"""Small explicit Skill loader used by the native runtime."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


_HEADER = re.compile(r"^---\n(?P<body>.*?)\n---\n(?P<content>.*)$", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    skill_id: str
    version: str
    content: str
    content_hash: str
    path: str

    def metadata(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "skill_version": self.version,
            "skill_content_hash": self.content_hash,
            "skill_path": self.path,
        }


def load_skill(path: Path | str) -> Skill:
    skill_path = Path(path)
    raw = skill_path.read_text(encoding="utf-8")
    match = _HEADER.match(raw)
    if not match:
        raise ValueError(f"skill must have a front matter header: {skill_path}")
    metadata: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"\'')
    skill_id = metadata.get("id", "").strip()
    version = metadata.get("version", "").strip()
    content = match.group("content").strip()
    if not skill_id or not version or not content:
        raise ValueError(f"skill requires id, version, and content: {skill_path}")
    return Skill(
        skill_id=skill_id,
        version=version,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        path=str(skill_path),
    )
