#!/usr/bin/env python3
"""Tests for Phase 7B-3 FrontendAgent."""

import json
from unittest.mock import patch

from contracts import FrontendRequest, FrontendResult
from agents.frontend import FrontendAgent
from skills.loader import skill_loader


class MockConfig:
    pass


def test_frontend_agent_imports():
    from agents.frontend import FrontendAgent
    assert FrontendAgent is not None


def test_generate_frontend_success():
    agent = FrontendAgent(MockConfig())
    fake_response = json.dumps({
        "html": "<section>Hello</section>",
        "css": ".hero { padding: 40px; }",
        "javascript": "",
        "animation_used": False,
        "gsap_subskills_loaded": [],
        "warnings": [],
    })

    with patch.object(agent, 'call_ai', return_value=fake_response) as mock:
        request = FrontendRequest(
            task_id="test-1",
            frontend_scope="page",
            animation_required=False,
            design_brief="Minimalist landing page",
        )
        result = agent.generate_frontend(request)

    assert result.success is True
    assert result.task_id == "test-1"
    assert result.html == "<section>Hello</section>"
    assert result.css == ".hero { padding: 40px; }"
    assert result.javascript == ""
    assert result.animation_used is False
    assert result.gsap_subskills_loaded == []
    assert result.validation_status == "pending"
    assert result.errors == []
    mock.assert_called_once()


def test_animation_disabled_no_gsap_skills():
    agent = FrontendAgent(MockConfig())
    fake_response = json.dumps({
        "html": "<section>Hello</section>",
        "css": "",
        "javascript": "",
        "animation_used": False,
        "gsap_subskills_loaded": [],
        "warnings": [],
    })

    with patch.object(agent, 'call_ai', return_value=fake_response) as mock:
        request = FrontendRequest(
            task_id="test-2",
            animation_required=False,
            design_brief="Test",
        )
        agent.generate_frontend(request)

    args, kwargs = mock.call_args
    required = kwargs.get('required_skills', [])
    for skill in required:
        assert not skill.startswith("gsap-"), f"GSAP skill should not be loaded when animation_required=False: {skill}"


def test_animation_enabled_loads_gsap_skills():
    agent = FrontendAgent(MockConfig())
    fake_response = json.dumps({
        "html": "<section>Hello</section>",
        "css": "",
        "javascript": "gsap.to('.box', { x: 100 });",
        "animation_used": True,
        "gsap_subskills_loaded": ["gsap-core", "gsap-timeline", "gsap-scrolltrigger", "gsap-utils"],
        "warnings": [],
    })

    with patch.object(agent, 'call_ai', return_value=fake_response) as mock:
        request = FrontendRequest(
            task_id="test-3",
            animation_required=True,
            design_brief="Test with animation",
        )
        agent.generate_frontend(request)

    args, kwargs = mock.call_args
    required = kwargs.get('required_skills', [])
    assert "gsap-core" in required
    assert "gsap-timeline" in required
    assert "gsap-scrolltrigger" in required
    assert "gsap-utils" in required
    assert "gsap-react" not in required
    assert "gsap-frameworks" not in required


def test_taste_not_fully_loaded():
    agent = FrontendAgent(MockConfig())
    fake_response = json.dumps({
        "html": "<section>Hello</section>",
        "css": "",
        "javascript": "",
        "animation_used": False,
        "gsap_subskills_loaded": [],
        "warnings": [],
    })

    with patch.object(agent, 'call_ai', return_value=fake_response) as mock:
        request = FrontendRequest(
            task_id="test-4",
            animation_required=False,
            design_brief="Test",
        )
        agent.generate_frontend(request)

    args, kwargs = mock.call_args
    required = kwargs.get('required_skills', [])

    taste_skills = [s for s in required if s.startswith("design-taste-frontend")]
    assert len(taste_skills) > 0, "At least one taste section should be loaded"
    for skill in taste_skills:
        assert ":" in skill, f"Taste skill must use section syntax: {skill}"
        assert skill != "design-taste-frontend", "Bare design-taste-frontend should not be loaded"
        assert skill.startswith("design-taste-frontend:")

    sections = skill_loader.get_skill_sections("design-taste-frontend")
    for skill in taste_skills:
        section_id = skill.split(":", 1)[1]
        assert section_id in sections, f"Section ID '{section_id}' does not exist in design-taste-frontend"


def test_invalid_json_returns_error():
    agent = FrontendAgent(MockConfig())
    with patch.object(agent, 'call_ai', return_value="This is not valid JSON"):
        request = FrontendRequest(
            task_id="test-5",
            animation_required=False,
            design_brief="Test",
        )
        result = agent.generate_frontend(request)

    assert result.success is False
    assert len(result.errors) > 0
    assert "parse" in result.errors[0].lower() or "json" in result.errors[0].lower()


def test_ai_failure_returns_error():
    agent = FrontendAgent(MockConfig())
    with patch.object(agent, 'call_ai', side_effect=RuntimeError("AI service down")):
        request = FrontendRequest(
            task_id="test-6",
            animation_required=False,
            design_brief="Test",
        )
        result = agent.generate_frontend(request)

    assert result.success is False
    assert len(result.errors) > 0
    assert "AI service down" in result.errors[0]


def test_no_greenlight_converter_import():
    import agents.frontend as frontend_module
    assert "GreenLightConverter" not in dir(frontend_module)


def test_optional_fields_handled():
    agent = FrontendAgent(MockConfig())
    fake_response = json.dumps({
        "html": "<section>Hello</section>",
        "css": ".hero { color: red; }",
        "javascript": "",
        "animation_used": False,
        "gsap_subskills_loaded": [],
        "warnings": ["test warning"],
    })

    with patch.object(agent, 'call_ai', return_value=fake_response):
        request = FrontendRequest(
            task_id="test-7",
            frontend_scope="section",
            animation_required=False,
            design_brief="Minimalist",
            brand_constraints={"primary": "#2C2420"},
            seo_title="Test Title",
            seo_description="Test description",
            seo_keywords=["test", "minimalist"],
            content_outline=[{"type": "hero", "content": "Hello"}],
        )
        result = agent.generate_frontend(request)

    assert result.success is True
    assert result.html == "<section>Hello</section>"
    assert result.css == ".hero { color: red; }"
    assert result.warnings == ["test warning"]


if __name__ == '__main__':
    test_frontend_agent_imports()
    test_generate_frontend_success()
    test_animation_disabled_no_gsap_skills()
    test_animation_enabled_loads_gsap_skills()
    test_taste_not_fully_loaded()
    test_invalid_json_returns_error()
    test_ai_failure_returns_error()
    test_no_greenlight_converter_import()
    test_optional_fields_handled()
    print('All FrontendAgent tests passed.')
