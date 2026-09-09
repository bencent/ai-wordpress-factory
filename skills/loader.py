import os
import re
from typing import Dict, List, Optional, Any


class Skill:
    def __init__(self, name: str, description: str, content: str, path: str, kind: str = "skill", parent: Optional[str] = None, metadata: Dict[str, Any] = None):
        self.name = name
        self.description = description
        self.content = content
        self.path = path
        self.kind = kind
        self.parent = parent
        self.metadata = metadata or {}
        self.sections: Dict[str, str] = {}
        self._size = len(content.encode("utf-8"))


_SKILL_METADATA = {
    "Bencent": {
        "category": "constitution",
        "priority": 1,
        "estimated_size": "small",
    },
    "design-taste-frontend": {
        "category": "design",
        "priority": 2,
        "estimated_size": "large",
    },
    "minimalist-ui": {
        "category": "design",
        "priority": 2,
        "estimated_size": "medium",
    },
    "ui-rules": {
        "category": "design",
        "priority": 2,
        "estimated_size": "small",
    },
    "humanizer-tw": {
        "category": "writing",
        "priority": 3,
        "estimated_size": "medium",
    },
    "greenlight-vibe": {
        "category": "production",
        "priority": 4,
        "estimated_size": "large",
        "tool_bindings": ["frontend"],
    },
    "gsap-core": {
        "category": "production",
        "priority": 4,
        "estimated_size": "medium",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-timeline": {
        "category": "production",
        "priority": 4,
        "estimated_size": "small",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-scrolltrigger": {
        "category": "production",
        "priority": 4,
        "estimated_size": "medium",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-plugins": {
        "category": "production",
        "priority": 4,
        "estimated_size": "large",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-utils": {
        "category": "production",
        "priority": 4,
        "estimated_size": "medium",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-react": {
        "category": "production",
        "priority": 4,
        "estimated_size": "small",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-performance": {
        "category": "production",
        "priority": 4,
        "estimated_size": "small",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
    "gsap-frameworks": {
        "category": "production",
        "priority": 4,
        "estimated_size": "medium",
        "tool_bindings": ["animation"],
        "namespace": "gsap",
    },
}

_TOOL_BINDINGS = {
    "frontend": ["greenlight-vibe"],
    "animation": ["gsap-core", "gsap-timeline", "gsap-scrolltrigger", "gsap-plugins", "gsap-utils", "gsap-react", "gsap-performance", "gsap-frameworks"],
}

PRIORITY_ORDER = {
    "constitution": 0,
    "design": 1,
    "writing": 2,
    "production": 3,
}

_SIZE_THRESHOLD = 50 * 1024  # 50 KB


def _normalize_section_id(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^#+\s*', '', text)
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'\s+', '-', text)
    return text.lower()


def _parse_sections(content: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    lines = content.splitlines()
    current_heading = None
    current_parts: List[str] = []
    section_order: List[str] = []

    for line in lines:
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_parts).strip()
            current_heading = _normalize_section_id(heading_match.group(2))
            current_parts = [line]
            section_order.append(current_heading)
        else:
            if current_heading is None:
                if line.strip():
                    current_heading = "_preamble"
                    section_order.append(current_heading)
            current_parts.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_parts).strip()

    ordered = {key: sections[key] for key in section_order if key in sections}
    return ordered


class SkillLoader:
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = skills_dir
        self._skills: Dict[str, Skill] = {}
        self._sections: Dict[str, Dict[str, str]] = {}
        self._load_all()

    def _parse_frontmatter(self, content: str) -> tuple[Dict[str, Any], str]:
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
        if not match:
            return {}, content
        frontmatter_text = match.group(1)
        body = match.group(2)
        metadata: Dict[str, Any] = {}
        for line in frontmatter_text.split('\n'):
            line = line.strip()
            if ':' in line:
                key, _, value = line.partition(':')
                key = key.strip()
                value = value.strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith('|'):
                    value = value[1:].strip()
                metadata[key] = value
        return metadata, body

    def _load_markdown_file(self, file_path: str) -> Optional[Skill]:
        if not os.path.isfile(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        except Exception:
            return None

        if not raw.strip():
            return None

        metadata, body = self._parse_frontmatter(raw)
        name = metadata.get("name")
        if not name:
            return None

        description = metadata.get("description", "")
        loader_meta = _SKILL_METADATA.get(name, {})
        merged_metadata = {**loader_meta, **metadata}
        return Skill(
            name=name,
            description=description,
            content=body.strip(),
            path=file_path,
            metadata=merged_metadata,
        )

    def _load_instruction_files(self, skill_name: str, skill_path: str, skill: Skill) -> None:
        instructions_dir = os.path.join(skill_path, "instructions")
        if not os.path.isdir(instructions_dir):
            return
        for entry in os.listdir(instructions_dir):
            if not entry.endswith(".md"):
                continue
            topic = entry[:-3]
            topic_path = os.path.join(instructions_dir, entry)
            topic_skill = self._load_markdown_file(topic_path)
            if topic_skill is None:
                try:
                    with open(topic_path, "r", encoding="utf-8-sig") as f:
                        raw = f.read()
                    _, body = self._parse_frontmatter(raw)
                    topic_skill = Skill(
                        name=f"{skill_name}:{topic}",
                        description="",
                        content=body.strip(),
                        path=topic_path,
                        kind="instruction",
                        parent=skill_name,
                    )
                except Exception:
                    continue
            else:
                topic_skill.name = f"{skill_name}:{topic}"
                topic_skill.parent = skill_name
                topic_skill.kind = "instruction"
            self._skills[topic_skill.name] = topic_skill
            skill.sections[topic] = topic_skill.content

    def _load_all(self) -> None:
        if not os.path.isdir(self.skills_dir):
            return
        for root, dirs, files in os.walk(self.skills_dir):
            dirs.sort()
            for filename in sorted(files):
                if filename != "SKILL.md":
                    continue
                file_path = os.path.join(root, filename)
                skill = self._load_markdown_file(file_path)
                if not skill:
                    continue

                rel_dir = os.path.relpath(root, self.skills_dir)
                if rel_dir in (".", ".."):
                    rel_dir = os.path.basename(root)

                skill_parent = None
                parts = rel_dir.replace("\\", "/").split("/")
                if len(parts) > 1:
                    skill_parent = parts[0]
                    if len(parts) == 2:
                        skill.name = f"{parts[0]}-{parts[1]}" if skill.name != parts[1] else parts[1]
                    else:
                        skill.name = f"{parts[0]}-{parts[-1]}" if skill.name != parts[-1] else parts[-1]

                if skill_parent:
                    skill.parent = skill_parent

                self._skills[skill.name] = skill
                skill.sections = _parse_sections(skill.content)
                self._sections[skill.name] = skill.sections

                if skill.name in _SKILL_METADATA:
                    instructions_parent = skill.metadata.get("name", skill.name)
                else:
                    instructions_parent = skill.name
                self._load_instruction_files(instructions_parent, root, skill)

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_skills(self) -> List[str]:
        return list(self._skills.keys())

    def get_constitution(self) -> Optional[Skill]:
        for name in ("Bencent", "bencent-brand"):
            skill = self._skills.get(name)
            if skill:
                return skill
        return None

    def build_constitution_context(self) -> str:
        skill = self.get_constitution()
        if not skill:
            return ""
        parts = ["## Constitution\n"]
        parts.append(f"### {skill.name}\n{skill.description}\n")
        parts.append(skill.content)
        parts.append("\n")
        return "\n".join(parts)

    def get_skills(self, names: List[str]) -> List[Skill]:
        seen = set()
        skills = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            skill = self._skills.get(name)
            if skill:
                skills.append(skill)
        return skills

    def _skill_sort_key(self, skill: Skill):
        meta = _SKILL_METADATA.get(skill.name, {})
        category = meta.get("category", "writing")
        priority = meta.get("priority", 999)
        return (PRIORITY_ORDER.get(category, 999), priority)

    def get_skills_for_agent(self, agent_name: str) -> List[Skill]:
        mapping = {
            "planner": ["Bencent"],
            "research": ["Bencent"],
            "writer": ["Bencent", "humanizer-tw"],
            "critic": ["Bencent", "humanizer-tw"],
            "seo": ["Bencent"],
            "reviewer": ["Bencent", "humanizer-tw"],
            "qualityevaluator": ["Bencent", "humanizer-tw"],
            "contentfixer": ["Bencent"],
            "finalreviewer": ["Bencent", "humanizer-tw"],
            "router": ["Bencent"],
            "image": ["Bencent", "minimalist-ui"],
            "learner": ["Bencent"],
        }
        skill_names = mapping.get(agent_name, ["bencent-brand"])
        return self.get_skills(skill_names)

    def _build_skill_block(self, skill: Skill, section: Optional[str] = None) -> str:
        if section and section in skill.sections:
            content = skill.sections[section]
        else:
            content = skill.content
        return f"### {skill.name}\n{skill.description}\n{content}\n"

    def build_skills_context(self, agent_name: str, required_skills: Optional[List[str]] = None) -> str:
        constitution = self.get_constitution()
        agent_defaults = self.get_skills_for_agent(agent_name)
        explicit = self.get_skills(required_skills) if required_skills else []

        combined = []
        seen = set()

        if constitution and constitution.name not in seen:
            combined.append((constitution, None))
            seen.add(constitution.name)

        if required_skills:
            for skill, section in self._resolve_required_skills(required_skills):
                if skill.name not in seen:
                    combined.append((skill, section))
                    seen.add(skill.name)
        else:
            for skill in agent_defaults:
                if skill.name not in seen:
                    combined.append((skill, None))
                    seen.add(skill.name)

        non_constitution = [(s, sec) for s, sec in combined if s.name != "Bencent"]
        non_constitution.sort(key=lambda item: self._skill_sort_key(item[0]))

        result = []
        if constitution:
            result.append((constitution, None))
        result.extend(non_constitution)

        if not result:
            return ""

        parts = ["## Active Skills Context\n", "Higher-priority rules override lower-priority rules when conflicts occur.\n\n"]
        for skill, section in result:
            parts.append(self._build_skill_block(skill, section))
        return "\n".join(parts)

    def _resolve_required_skills(self, required_skills: List[str]) -> List[tuple]:
        resolved = []
        for spec in required_skills:
            if ":" in spec:
                skill_name, section_id = spec.split(":", 1)
                skill = self._skills.get(skill_name)
                if skill:
                    resolved.append((skill, section_id))
            else:
                skill = self._skills.get(spec)
                if skill:
                    resolved.append((skill, None))
        return resolved

    def get_tool_skills(self, tool_name: str) -> List[Skill]:
        skill_names = _TOOL_BINDINGS.get(tool_name, [])
        return self.get_skills(skill_names)

    def get_skill_sections(self, name: str) -> Dict[str, str]:
        skill = self._skills.get(name)
        if not skill:
            return {}
        return dict(skill.sections)


skill_loader = SkillLoader()
