"""Mtime-Cached Dynamic Skill Registry and Hermes-style Closed Learning Loop (`SKILL.md`)."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclasses.dataclass
class SkillMetadata:
    name: str
    description: str
    skill_dir: pathlib.Path
    body: str
    resource_files: List[str]
    enabled: bool = True


class SkillRegistry:
    """Discovers, caches by `st_mtime_ns`, and dynamically saves reusable `SKILL.md` capabilities."""

    def __init__(self, skills_dir: pathlib.Path) -> None:
        self.skills_dir = skills_dir
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        self._skills: Dict[str, SkillMetadata] = {}
        self._last_snapshot_sig: Optional[Tuple[Tuple[str, int], ...]] = None
        self._cached_catalog_prompt: str = "No custom skills installed yet."
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.reload(force=True)

    def is_writable(self) -> bool:
        return os.access(self.skills_dir, os.W_OK)

    def _compute_dir_signature(self) -> Tuple[Tuple[str, int], ...]:
        if not self.skills_dir.exists():
            return ()
        sig = []
        for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                sig.append((str(skill_md), skill_md.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(sig)

    def reload(self, force: bool = False) -> bool:
        """Re-parses `SKILL.md` files ONLY if a file was added, removed, or modified on disk."""
        current_sig = self._compute_dir_signature()
        if not force and self._last_snapshot_sig == current_sig:
            self.cache_hits += 1
            return False

        self.cache_misses += 1
        self._last_snapshot_sig = current_sig
        self._skills.clear()
        for path_str, _ in current_sig:
            skill_md = pathlib.Path(path_str)
            try:
                meta = self._parse_skill_md(skill_md)
            except OSError:
                continue
            if meta and meta.enabled:
                self._skills[meta.name.lower()] = meta

        if not self._skills:
            self._cached_catalog_prompt = "No custom skills installed yet."
        else:
            self._cached_catalog_prompt = "\n".join(
                f"- **{meta.name}**: {meta.description}" for meta in self._skills.values()
            )
        return True

    def _parse_skill_md(self, path: pathlib.Path) -> Optional[SkillMetadata]:
        raw = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(raw)
        name = path.parent.name
        description = "No description provided."
        enabled = True
        body = raw
        if match:
            fm_text, body = match.group(1), match.group(2)
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k, v = k.strip().lower(), v.strip().strip("'\"")
                    if k == "name" and v:
                        name = v
                    elif k == "description" and v:
                        description = v
                    elif k == "enabled":
                        enabled = v.lower() not in ("false", "0", "no")
        resources = [
            str(p.relative_to(path.parent))
            for p in path.parent.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        ]
        return SkillMetadata(
            name=name,
            description=description,
            skill_dir=path.parent,
            body=body.strip(),
            resource_files=sorted(resources),
            enabled=enabled,
        )

    def list_catalog_prompt(self) -> str:
        """Returns cached compact skill descriptions for system prompt injection."""
        self.reload(force=False)
        return self._cached_catalog_prompt

    def lookup_skills(self, skill_names: List[str]) -> Dict[str, Any]:
        self.reload(force=False)
        found = []
        missing = []
        for requested in skill_names:
            clean = requested.lower().replace("user:", "").replace("system:", "").strip()
            meta = self._skills.get(clean)
            if not meta:
                candidates = [
                    m
                    for k, m in self._skills.items()
                    if clean in k or clean in m.description.lower()
                ]
                if len(candidates) == 1:
                    meta = candidates[0]
            if meta:
                found.append(
                    {
                        "name": meta.name,
                        "description": meta.description,
                        "resources": meta.resource_files,
                        "instructions": meta.body,
                    }
                )
            else:
                missing.append(requested)
        return {"loaded_skills": found, "not_found": missing}

    def get_skill_resources(self, skill_name: str, filenames: List[str]) -> Dict[str, str]:
        self.reload(force=False)
        clean = skill_name.lower().replace("user:", "").replace("system:", "").strip()
        meta = self._skills.get(clean)
        if not meta:
            return {"error": f"Skill not found: {skill_name}"}
        output: Dict[str, str] = {}
        for fname in filenames:
            target = (meta.skill_dir / fname).resolve()
            if not str(target).startswith(str(meta.skill_dir.resolve())) or not target.is_file():
                output[fname] = f"Error: Resource {fname} not found inside skill {skill_name}"
            else:
                output[fname] = target.read_text(encoding="utf-8", errors="replace")
        return output

    def save_skill(
        self,
        name: str,
        description: str,
        instructions_markdown: str,
        scripts: Optional[Dict[str, str]] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
        target_dir = self.skills_dir / slug
        if target_dir.exists() and not overwrite:
            return {
                "status": "already_exists",
                "name": slug,
                "message": f"Skill '{slug}' already exists. Pass overwrite=True after user confirmation.",
            }
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            skill_md_content = (
                f"---\nname: {slug}\ndescription: {description}\nenabled: true\n---\n\n"
                f"{instructions_markdown.strip()}\n"
            )
            (target_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
            if scripts:
                for rel_path, code in scripts.items():
                    script_file = (target_dir / rel_path).resolve()
                    if str(script_file).startswith(str(target_dir.resolve())):
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(code, encoding="utf-8")
                        if script_file.suffix in (".sh", ".py"):
                            script_file.chmod(0o755)
        except PermissionError as exc:
            return {
                "status": "permission_denied",
                "name": slug,
                "skills_dir": str(self.skills_dir),
                "error": str(exc),
                "remediation": (
                    f"The Linux user running Ouroboros lacks write permission on '{self.skills_dir}'. "
                    f"Fix on host: sudo chown -R $(id -un):$(id -gn) {self.skills_dir} && sudo chmod -R u+rwX {self.skills_dir}"
                ),
            }
        self.reload(force=True)
        return {"status": "created", "name": slug, "path": str(target_dir)}
