import json
from typing import Optional, Dict, Any, List
from contracts import FrontendRequest, FrontendResult
from skills.loader import skill_loader
from . import BaseAgent


class FrontendAgent(BaseAgent):
    """FrontendAgent，負責根據 FrontendRequest 生成 HTML/CSS/JavaScript。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "根據 FrontendRequest 生成前端 HTML/CSS/JavaScript 與動畫指引。"

    def generate_frontend(
        self,
        request: FrontendRequest,
    ) -> FrontendResult:
        required_skills = self._build_required_skills(request)
        prompt = self._build_frontend_prompt(request)

        try:
            raw = self.call_ai(prompt, required_skills=required_skills)
        except Exception as e:
            return FrontendResult(
                task_id=request.task_id,
                success=False,
                errors=[str(e)],
            )

        try:
            data = self._parse_ai_response(raw)
        except Exception as e:
            return FrontendResult(
                task_id=request.task_id,
                success=False,
                errors=[f"Failed to parse AI response: {str(e)}"],
            )

        return FrontendResult(
            task_id=request.task_id,
            success=True,
            html=data.get("html"),
            css=data.get("css"),
            javascript=data.get("javascript"),
            animation_used=data.get("animation_used", False),
            gsap_subskills_loaded=data.get("gsap_subskills_loaded", []),
            warnings=data.get("warnings", []),
            validation_status="pending",
        )

    def _build_required_skills(self, request: FrontendRequest) -> List[str]:
        skills: List[str] = ["minimalist-ui", "ui-rules"]

        taste_sections = skill_loader.get_skill_sections("design-taste-frontend")
        if taste_sections:
            selected = self._select_taste_sections(taste_sections)
            for section_id in selected:
                skills.append(f"design-taste-frontend:{section_id}")

        if request.animation_required:
            skills.extend([
                "gsap-core",
                "gsap-timeline",
                "gsap-scrolltrigger",
                "gsap-utils",
            ])

        return skills

    def _select_taste_sections(self, sections: Dict[str, str]) -> List[str]:
        preferred = [
            "0-brief-inference-read-the-room-before-anything-else",
            "1-the-three-dials-core-configuration",
            "3-default-architecture-conventions",
            "4-design-engineering-directives-bias-correction",
            "9-ai-tells-forbidden-patterns",
            "14-final-pre-flight-check",
        ]
        return [section_id for section_id in preferred if section_id in sections]

    def _build_frontend_prompt(self, request: FrontendRequest) -> str:
        parts: List[str] = []

        parts.append(f"Frontend scope: {request.frontend_scope}")
        parts.append(f"Design brief: {request.design_brief}")

        if request.brand_constraints:
            parts.append(
                f"Brand constraints: {json.dumps(request.brand_constraints, ensure_ascii=False)}"
            )

        seo_parts: List[str] = []
        if request.seo_title:
            seo_parts.append(f"SEO title: {request.seo_title}")
        if request.seo_description:
            seo_parts.append(f"SEO description: {request.seo_description}")
        if request.seo_keywords:
            seo_parts.append(f"SEO keywords: {', '.join(request.seo_keywords)}")
        if seo_parts:
            parts.append("\n".join(seo_parts))

        if request.content_outline:
            parts.append(
                f"Content outline: {json.dumps(request.content_outline, ensure_ascii=False, indent=2)}"
            )

        if request.animation_required:
            parts.append(
                "Animation guidance: Use GSAP only when animation provides meaningful UX value. "
                "Avoid decorative animation. Respect performance and reduced-motion considerations."
            )
        else:
            parts.append("Animation guidance: Do not generate GSAP animation.")

        prompt = (
            "You are a frontend generation assistant. "
            "Generate HTML, CSS, and optionally JavaScript based on the following request.\n\n"
        )
        prompt += "\n\n".join(parts)
        prompt += "\n\nReturn ONLY a JSON object with this exact structure:\n"
        prompt += json.dumps(
            {
                "html": "<string>",
                "css": "<string>",
                "javascript": "<string>",
                "animation_used": False,
                "gsap_subskills_loaded": [],
                "warnings": [],
            },
            indent=2,
        )
        prompt += "\n\nJavaScript is optional. Return an empty string when no JavaScript is needed."

        return prompt

    def _parse_ai_response(self, raw: str) -> Dict[str, Any]:
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON response: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError("AI response is not a JSON object")

        return data
