#!/usr/bin/env python3
"""Skill loader diagnostics and behavioral checks."""

from skills.loader import skill_loader
from agents.writer import WriterAgent
from agents.critic import CriticAgent
from agents.quality_evaluator import QualityEvaluatorAgent
from agents.content_fixer import ContentFixerAgent
from agents.final_reviewer import FinalReviewerAgent
from agents.image import ImageAgent
from agents.planner import PlannerAgent
from agents.research import ResearchAgent
from agents.seo import SEOAgent
from agents.router import Router
from agents.learner import LearnerAgent


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def check(name, condition, message=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f": {message}" if message else ""))
    return bool(condition)


def main():
    all_passed = True

    print("=== Skill Discovery ===")
    skills = skill_loader.list_skills()
    all_passed &= check("Skills discovered", len(skills) >= 14, f"found {len(skills)}")
    for expected in ["Bencent", "design-taste-frontend", "minimalist-ui", "humanizer-tw", "greenlight-vibe", "ui-rules"]:
        all_passed &= check(f"Skill present: {expected}", expected in skills)
    # gsap sub-skills are now discovered via recursive scan
    gsap_subskills = [s for s in skills if s.startswith("gsap-")]
    all_passed &= check("gsap sub-skills discovered", len(gsap_subskills) >= 8, f"found {gsap_subskills}")

    print("\n=== Constitution ===")
    constitution = skill_loader.get_constitution()
    all_passed &= check("Constitution exists", constitution is not None)
    all_passed &= check("Constitution is Bencent", constitution and constitution.name == "Bencent")
    ctx = skill_loader.build_constitution_context()
    all_passed &= check("Constitution context non-empty", bool(ctx))
    all_passed &= check("Constitution appears first", ctx.startswith("## Constitution\n"))

    print("\n=== Priority / Ordering ===")
    image_default = skill_loader.get_skills_for_agent("image")
    image_names = [s.name for s in image_default]
    all_passed &= check("Image default skills", image_names == ["Bencent", "minimalist-ui"], str(image_names))

    writer_default = skill_loader.get_skills_for_agent("writer")
    writer_names = [s.name for s in writer_default]
    all_passed &= check("Writer default skills", writer_names == ["Bencent", "humanizer-tw"], str(writer_names))

    # Explicit selection with dedup
    explicit = skill_loader.build_skills_context("writer", required_skills=["humanizer-tw", "Bencent", "humanizer-tw"])
    count_bencent = explicit.count("### Bencent")
    count_humanizer = explicit.count("### humanizer-tw")
    all_passed &= check("Dedup: Bencent once", count_bencent == 1, f"count={count_bencent}")
    all_passed &= check("Dedup: humanizer-tw once", count_humanizer == 1, f"count={count_humanizer}")

    # Priority ordering: constitution before others
    mixed = skill_loader.build_skills_context("writer", required_skills=["design-taste-frontend", "Bencent"])
    bencent_pos = mixed.find("### Bencent")
    taste_pos = mixed.find("### design-taste-frontend")
    all_passed &= check("Constitution before design", bencent_pos < taste_pos, f"Bencent={bencent_pos}, Taste={taste_pos}")

    # Conflict instruction present
    all_passed &= check("Conflict instruction present", "Higher-priority rules override lower-priority rules" in mixed)

    print("\n=== Tool Bindings ===")
    frontend_tools = skill_loader.get_tool_skills("frontend")
    frontend_names = [s.name for s in frontend_tools]
    all_passed &= check("Frontend tool skills include greenlight-vibe", "greenlight-vibe" in frontend_names, str(frontend_names))

    animation_tools = skill_loader.get_tool_skills("animation")
    animation_names = [s.name for s in animation_tools]
    all_passed &= check("Animation tool skills include gsap sub-skills", len(animation_names) >= 8, f"found {len(animation_names)}: {animation_names[:3]}...")

    print("\n=== Agent Defaults ===")
    for agent_cls, expected in [
        (PlannerAgent, ["Bencent"]),
        (ResearchAgent, ["Bencent"]),
        (SEOAgent, ["Bencent"]),
        (Router, ["Bencent"]),
        (LearnerAgent, ["Bencent"]),
        (ContentFixerAgent, ["Bencent"]),
        (CriticAgent, ["Bencent", "humanizer-tw"]),
        (QualityEvaluatorAgent, ["Bencent", "humanizer-tw"]),
        (FinalReviewerAgent, ["Bencent", "humanizer-tw"]),
        (ImageAgent, ["Bencent", "minimalist-ui"]),
    ]:
        agent = agent_cls(None)
        actual = [s.name for s in skill_loader.get_skills_for_agent(agent.name)]
        all_passed &= check(f"{agent.name} default skills", actual == expected, f"got {actual}")

    print("\n=== Granular Selection ===")
    # Writer default context size
    writer_agent = WriterAgent(None)
    default_size = estimate_tokens(writer_agent.skills_context)
    print(f"Writer default skills context: ~{default_size} tokens")

    # Writer explicit minimal
    minimal = skill_loader.build_skills_context("writer", required_skills=["Bencent"])
    minimal_size = estimate_tokens(minimal)
    print(f"Writer explicit [Bencent] context: ~{minimal_size} tokens")
    all_passed &= check("Explicit selection smaller than default", minimal_size < default_size, f"{minimal_size} < {default_size}")

    # ImageAgent context size
    image_agent = ImageAgent(None)
    image_size = estimate_tokens(image_agent.skills_context)
    print(f"ImageAgent skills context: ~{image_size} tokens")
    all_passed &= check("ImageAgent context under 25k tokens", image_size < 25000, f"got {image_size}")

    # QualityEvaluator explicit minimal
    qe_minimal = skill_loader.build_skills_context("qualityevaluator", required_skills=["Bencent"])
    qe_minimal_size = estimate_tokens(qe_minimal)
    qe_default_size = estimate_tokens(skill_loader.build_skills_context("qualityevaluator"))
    print(f"QualityEvaluator default: ~{qe_default_size} tokens, explicit [Bencent]: ~{qe_minimal_size} tokens")
    all_passed &= check("QE explicit smaller than default", qe_minimal_size < qe_default_size, f"{qe_minimal_size} < {qe_default_size}")

    print("\n=== Metadata ===")
    for name, meta in [
        ("Bencent", {"category": "constitution", "priority": 1}),
        ("design-taste-frontend", {"category": "design", "priority": 2}),
        ("minimalist-ui", {"category": "design", "priority": 2}),
        ("humanizer-tw", {"category": "writing", "priority": 3}),
        ("greenlight-vibe", {"category": "production", "priority": 4}),
    ]:
        skill = skill_loader.get_skill(name)
        all_passed &= check(f"Metadata {name} category", skill and skill.metadata.get("category") == meta["category"])
        all_passed &= check(f"Metadata {name} priority", skill and str(skill.metadata.get("priority")) == str(meta["priority"]))

    print("\n=== Recursive Discovery ===")
    all_passed &= check("gsap-core discovered", skill_loader.get_skill("gsap-core") is not None)
    all_passed &= check("gsap-scrolltrigger discovered", skill_loader.get_skill("gsap-scrolltrigger") is not None)
    all_passed &= check("gsap-plugins discovered", skill_loader.get_skill("gsap-plugins") is not None)
    all_passed &= check("gsap-parent is namespace", skill_loader.get_skill("gsap-core") and skill_loader.get_skill("gsap-core").parent == "gsap")

    print("\n=== GreenLight Instructions ===")
    gl = skill_loader.get_skill("greenlight-vibe")
    all_passed &= check("GreenLight main skill exists", gl is not None)
    all_passed &= check("GreenLight instructions discovered", "greenlight-vibe:core-structure" in skill_loader.list_skills())
    all_passed &= check("GreenLight charts discovered", "greenlight-vibe:charts" in skill_loader.list_skills())
    all_passed &= check("GreenLight scripts NOT in skills list", "greenlight-vibe:convert" not in skill_loader.list_skills())
    gl_context = skill_loader.build_skills_context("writer", required_skills=["greenlight-vibe:core-structure"])
    all_passed &= check("GreenLight instruction content in context", "wp:greenshift-blocks" in gl_context)
    all_passed &= check("GreenLight JS not in context", "convert.js" not in gl_context)

    print("\n=== Taste Sections ===")
    taste = skill_loader.get_skill("design-taste-frontend")
    all_passed &= check("Taste discovered", taste is not None)
    sections = skill_loader.get_skill_sections("design-taste-frontend")
    all_passed &= check("Taste has sections", len(sections) > 0, f"found {len(sections)} sections")
    all_passed &= check("Taste section: 9-ai-tells-forbidden-patterns", "9-ai-tells-forbidden-patterns" in sections)
    all_passed &= check("Taste section: 0-brief-inference-read-the-room-before-anything-else", "0-brief-inference-read-the-room-before-anything-else" in sections)
    taste_section_context = skill_loader.build_skills_context("image", required_skills=["design-taste-frontend:9-ai-tells-forbidden-patterns"])
    all_passed &= check("Taste section content in context", "AI TELLS" in taste_section_context)
    all_passed &= check("Taste section smaller than full", len(taste_section_context) < len(taste.content) if taste else False)

    print("\n=== Sub-skill Selection ===")
    gsap_context = skill_loader.build_skills_context("image", required_skills=["gsap-scrolltrigger"])
    all_passed &= check("gsap-scrolltrigger in context", "gsap-scrolltrigger" in gsap_context)
    all_passed &= check("gsap-scrolltrigger only sub-skill", gsap_context.count("gsap-scrolltrigger") == 1)

    print("\n=== Backward Compatibility ===")
    backward_checks = [
        (["Bencent"], ["Bencent"]),
        (["humanizer-tw"], ["humanizer-tw"]),
        (["design-taste-frontend"], ["design-taste-frontend"]),
        (["minimalist-ui"], ["minimalist-ui"]),
        (["ui-rules"], ["ui-rules"]),
    ]
    for required, expected_names in backward_checks:
        ctx = skill_loader.build_skills_context("writer", required_skills=required)
        for name in expected_names:
            all_passed &= check(f"Backward compat {name}", name in ctx, f"required={required}")

    print("\n=== Summary ===")
    if all_passed:
        print("All checks passed.")
    else:
        print("Some checks failed.")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
