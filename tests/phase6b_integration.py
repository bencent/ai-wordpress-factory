#!/usr/bin/env python3
"""Phase 6B Agent integration tests."""

import inspect
from unittest.mock import patch

from skills.loader import skill_loader
from agents.image import ImageAgent
from agents.writer import WriterAgent
from agents.quality_evaluator import QualityEvaluatorAgent
from agents.content_fixer import ContentFixerAgent
from agents.final_reviewer import FinalReviewerAgent
from agents.critic import CriticAgent
from agents.planner import PlannerAgent
from agents.research import ResearchAgent
from agents.seo import SEOAgent
from agents.learner import LearnerAgent


def test_imageagent_uses_explicit_skills():
    img = ImageAgent(None)
    with patch.object(img, 'call_ai', return_value='test prompt') as mock:
        img._build_image_prompt('Test Title', 'Test content')
        args, kwargs = mock.call_args
        required = kwargs.get('required_skills', [])
        assert 'Bencent' in required
        assert 'minimalist-ui' in required
        assert 'design-taste-frontend:48-image-visual-asset-strategy' in required
        assert 'design-taste-frontend:9-ai-tells-forbidden-patterns' in required
        assert 'design-taste-frontend:9a-visual-css' in required
        assert 'design-taste-frontend:42-color-calibration' in required
        assert 'design-taste-frontend:9f-production-test-tells-banned-outright' in required
        assert 'design-taste-frontend:hero-paradigms' in required


def test_imageagent_default_context_small():
    img = ImageAgent(None)
    tokens = len(img.skills_context) // 4
    assert tokens < 5000, f"ImageAgent default context too large: ~{tokens} tokens"


def test_imageagent_prompt_context_excludes_full_taste():
    ctx = skill_loader.build_skills_context('image', required_skills=[
        'Bencent',
        'design-taste-frontend:48-image-visual-asset-strategy',
        'design-taste-frontend:9-ai-tells-forbidden-patterns',
        'design-taste-frontend:9a-visual-css',
        'design-taste-frontend:42-color-calibration',
        'design-taste-frontend:9f-production-test-tells-banned-outright',
        'design-taste-frontend:hero-paradigms',
        'minimalist-ui',
    ])
    assert len(ctx) < 20000


def test_writer_refine_uses_explicit_bencent():
    from state import Task, ContentType
    writer = WriterAgent(None)
    task = Task(id='test-1', title='t', description='d', content_type=ContentType.BLOG_POST)
    task.draft_content = 'draft'
    with patch.object(writer, 'call_ai', return_value='refined') as mock:
        writer.refine_content(task, 'feedback')
        args, kwargs = mock.call_args
        required = kwargs.get('required_skills', [])
        assert required == ['Bencent']


def test_quality_evaluator_uses_explicit_bencent():
    from state import Task, ContentType
    qe = QualityEvaluatorAgent(None)
    task = Task(id='test-2', title='t', description='d', content_type=ContentType.BLOG_POST)
    with patch.object(qe, 'call_ai', return_value='7') as mock:
        qe._score_content('content', task)
        args, kwargs = mock.call_args
        required = kwargs.get('required_skills', [])
        assert required == ['Bencent']


def test_constitution_appears_first():
    ctx = skill_loader.build_skills_context('image', required_skills=[
        'gsap-scrolltrigger',
        'design-taste-frontend:48-image-visual-asset-strategy',
    ])
    bencent_pos = ctx.find('### Bencent')
    gsap_pos = ctx.find('### gsap-scrolltrigger')
    assert bencent_pos >= 0, 'Bencent not found in context'
    assert gsap_pos >= 0, 'gsap-scrolltrigger not found in context'
    assert bencent_pos < gsap_pos, f'Bencent ({bencent_pos}) should appear before gsap-scrolltrigger ({gsap_pos})'


def test_no_greenlight_or_gsap_in_default_agents():
    for cls in [WriterAgent, QualityEvaluatorAgent, ContentFixerAgent, FinalReviewerAgent, CriticAgent, PlannerAgent, ResearchAgent, SEOAgent, LearnerAgent]:
        agent = cls(None)
        ctx = agent.skills_context
        assert 'greenlight-vibe' not in ctx
        assert 'gsap-' not in ctx


if __name__ == '__main__':
    test_imageagent_uses_explicit_skills()
    test_imageagent_default_context_small()
    test_imageagent_prompt_context_excludes_full_taste()
    test_writer_refine_uses_explicit_bencent()
    test_quality_evaluator_uses_explicit_bencent()
    test_constitution_appears_first()
    test_no_greenlight_or_gsap_in_default_agents()
    print('All Phase 6B integration tests passed.')
