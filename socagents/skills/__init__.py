"""Skills: strategy playbooks packaged as ``SKILL.md`` files.

A skill is guidance for desk roles. It goes into a role's context as data under the fixed rules,
never into the system policy. It cannot add tools, network access, or code, and the deterministic
risk engine ignores it. Official skills ship with the package. User skills live in
``$SOCAGENTS_HOME/skills/<name>/SKILL.md`` and stay off until the user enables them.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from socagents.core.errors import ConfigError
from socagents.safety import looks_like_injection
from socagents.tools.catalog import full_registry
from socagents.tools.sdk import RiskClass

OFFICIAL_DIR = Path(__file__).parent / "official"
SKILL_FILE = "SKILL.md"
MAX_SKILL_BYTES = 8_000
SKILL_ROLES = ("bull", "bear", "strategist", "risk_officer", "desk_lead")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")

Source = Literal["official", "user"]

_PERFORMANCE_RE = re.compile(
    r"\b(win rate|guaranteed?|risk[- ]free|never lose|sure thing|"
    r"\d+(\.\d+)?\s?% (returns?|profits?|gains?|wins?))\b",
    re.IGNORECASE,
)
_OVERRIDE_RE = re.compile(
    r"\b(ignore|override|bypass|disable|skip) (the )?(risk|rules|policy|limits?|stops?)\b"
    r"|\bsize up\b|\bincrease (the )?(size|qty|quantity)\b",
    re.IGNORECASE,
)


class SkillManifest(BaseModel):
    name: str = Field(pattern=NAME_RE.pattern)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    version: str = Field(default="1", max_length=20)
    applies_to: list[str] = Field(default_factory=lambda: ["strategist"], min_length=1)
    tools: list[str] = Field(default_factory=list)

    @field_validator("applies_to")
    @classmethod
    def _known_roles(cls, value: list[str]) -> list[str]:
        unknown = [r for r in value if r not in SKILL_ROLES]
        if unknown:
            raise ValueError(
                f"unknown role(s) {', '.join(unknown)}; skills apply to {', '.join(SKILL_ROLES)}"
            )
        return value


@dataclass(frozen=True)
class Skill:
    manifest: SkillManifest
    body: str
    source: Source
    path: Path
    content_hash: str

    @property
    def name(self) -> str:
        return self.manifest.name

    def context_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.manifest.title,
            "source": self.source,
            "guidance": self.body,
        }


def _invalid(message: str) -> ConfigError:
    return ConfigError(message, code="invalid_skill")


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``SKILL.md`` into its ``key: value`` front matter and the Markdown body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise _invalid("SKILL.md must start with a front matter block between --- lines.")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise _invalid("The front matter block is not closed with ---.")
    meta: dict[str, Any] = {}
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise _invalid(f"Invalid front matter line: {line!r}.")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [
                v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()
            ]
        else:
            meta[key.strip()] = value.strip("'\"")
    return meta, "\n".join(lines[end + 1 :]).strip()


def load_skill(path: Path, source: Source) -> Skill:
    raw = path.read_bytes()
    if len(raw) > MAX_SKILL_BYTES:
        raise _invalid(f"{path.name} is larger than {MAX_SKILL_BYTES} bytes.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(f"{path.name} is not UTF-8 text.") from exc
    meta, body = parse_front_matter(text)
    try:
        manifest = SkillManifest.model_validate(meta)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(p) for p in first["loc"]) or "manifest"
        raise _invalid(f"Invalid skill manifest: {where}: {first['msg']}.") from exc
    return Skill(manifest, body, source, path, hashlib.sha256(raw).hexdigest())


def lint(skill: Skill) -> list[str]:
    """Reasons to reject a skill. Official skills must pass too."""
    problems: list[str] = []
    text = f"{skill.manifest.title}\n{skill.manifest.description}\n{skill.body}"
    if not skill.body:
        problems.append("it has no guidance text")
    if looks_like_injection(text):
        problems.append("it contains text that reads like instructions aimed at an agent")
    if _OVERRIDE_RE.search(text):
        problems.append("it tries to override risk rules or sizing")
    if _PERFORMANCE_RE.search(text):
        problems.append("it makes performance claims")
    registry = full_registry()
    for name in skill.manifest.tools:
        tool = registry.get(name)
        if tool is None:
            problems.append(f"it references an unknown tool {name!r}")
        elif tool.risk_class is not RiskClass.READ:
            problems.append(f"it references a tool that is not read-only: {name!r}")
    return problems


def discover(home: Path) -> tuple[dict[str, Skill], list[str]]:
    """Every loadable skill by name, and a problem line for each file that failed to load."""
    skills: dict[str, Skill] = {}
    problems: list[str] = []
    roots: tuple[tuple[Source, Path], ...] = (("official", OFFICIAL_DIR), ("user", home / "skills"))
    for source, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob(f"*/{SKILL_FILE}")):
            try:
                skill = load_skill(path, source)
            except ConfigError as exc:
                problems.append(f"{path}: {exc}")
                continue
            if path.parent.name != skill.name:
                problems.append(f"{path}: the folder name must match the skill name.")
            elif skill.name in skills:
                problems.append(f"{path}: {skill.name!r} is already an official skill.")
            else:
                skills[skill.name] = skill
    return skills, problems


def resolve_skills(home: Path, names: list[str], *, enabled: list[str]) -> list[Skill]:
    """Load the named skills for a run. User skills must also be in ``enabled``."""
    available, _ = discover(home)
    chosen: list[Skill] = []
    for name in dict.fromkeys(names):
        skill = available.get(name)
        if skill is None:
            raise ConfigError(
                f"Unknown skill {name!r}. See `socagents skills list`.", code="unknown_skill"
            )
        if skill.source == "user" and name not in enabled:
            raise ConfigError(
                f"{name} is a user skill. Review it, then enable it: socagents skills enable "
                f"{name}",
                code="skill_not_enabled",
            )
        problems = lint(skill)
        if problems:
            raise ConfigError(
                f"Skill {name} was rejected: {'; '.join(problems)}.", code="skill_rejected"
            )
        chosen.append(skill)
    return chosen


def add_user_skill(home: Path, source_path: Path) -> Skill:
    """Copy a reviewed-looking ``SKILL.md`` into the user skills folder. It stays disabled."""
    path = source_path / SKILL_FILE if source_path.is_dir() else source_path
    if not path.is_file():
        raise ConfigError(f"No skill file at {path}.", code="invalid_skill")
    skill = load_skill(path, "user")
    problems = lint(skill)
    if problems:
        raise ConfigError(
            f"Skill {skill.name} was rejected: {'; '.join(problems)}.", code="skill_rejected"
        )
    if (OFFICIAL_DIR / skill.name / SKILL_FILE).is_file():
        raise ConfigError(
            f"{skill.name!r} is the name of an official skill. Rename yours.", code="invalid_skill"
        )
    target = home / "skills" / skill.name / SKILL_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())
    return load_skill(target, "user")


def remove_user_skill(home: Path, name: str) -> bool:
    if not NAME_RE.match(name):
        raise ConfigError(f"Invalid skill name {name!r}.", code="invalid_skill")
    folder = home / "skills" / name
    target = folder / SKILL_FILE
    if not target.is_file():
        return False
    target.unlink()
    with contextlib.suppress(OSError):
        folder.rmdir()  # keeps any other files the user put there
    return True
