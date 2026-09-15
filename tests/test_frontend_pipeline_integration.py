#!/usr/bin/env python3
"""Workflow integration tests for Phase 7C-2 frontend pipeline."""

import unittest
from unittest.mock import Mock, patch
import base64
import json
import tempfile
from pathlib import Path

from state import Task, TaskStatus, ContentType, workflow_state
from contracts import (
    FrontendRequest, FrontendResult, FrontendSecurityResult,
    GreenLightConversionResult, FrontendValidationResult,
    ReviewResult, ReviewAction, CritiqueResult,
    ApprovalPolicy, ApprovalPolicyMode,
    BrandProductionRules, BrandProfile, ClientProfile,
    PreviewArtifact,
    VisualQualityResult, VisualQualityAction,
    ImageArtifact, ImageArtifactStatus, create_image_artifact,
)
from main import AIWordPressFactory


class TestFrontendPipelineIntegration(unittest.TestCase):
    """Integration tests for frontend pipeline in workflow."""

    def setUp(self):
        """Set up test fixtures."""
        self.factory = AIWordPressFactory()
        # Clear workflow state
        from state import workflow_state
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None
        self._temp_dir = tempfile.TemporaryDirectory()
        self._hero_image_path = Path(self._temp_dir.name) / "hero.png"
        self._hero_image_path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        ))

        # Patch VisualQualityReviewer to always return PASS by default
        self._visual_reviewer_patcher = patch("main.VisualQualityReviewer")
        visual_mock = self._visual_reviewer_patcher.start()
        visual_instance = Mock()
        visual_instance.review.return_value = VisualQualityResult(
            action=VisualQualityAction.PASS, summary="ok"
        )
        visual_mock.return_value = visual_instance

    def tearDown(self):
        self._visual_reviewer_patcher.stop()
        self._temp_dir.cleanup()

    def _create_test_task(self, **kwargs) -> Task:
        """Create a test task and return it."""
        task_id = self.factory.create_task(
            title="Test Blog Post",
            description="A test blog post for frontend pipeline",
            content_type=ContentType.BLOG_POST,
        )
        task = workflow_state.get_task(task_id)
        # Apply any custom attributes
        for key, value in kwargs.items():
            setattr(task, key, value)

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            local_path=str(self._hero_image_path),
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        return task

    def _create_mock_agents(self, frontend_agent=None, publisher=None):
        """Create mock agents for the workflow."""
        mock_planner = Mock()
        mock_planner.create_plan.return_value = {"outline": ["intro", "body", "conclusion"]}

        mock_research = Mock()
        mock_research.gather_research.return_value = [{"source": "test", "content": "research data"}]

        mock_writer = Mock()
        mock_writer.write_content.return_value = "Test draft content"
        mock_writer.call_ai.return_value = "Revised content"

        mock_critic = Mock()
        critique_result = CritiqueResult(
            score=85,
            issues=[],
            keep=["good structure"],
            rewrite=[],
            delete=[],
            research_more=[],
            overall_feedback="Good"
        )
        mock_critic.critique.return_value = critique_result

        mock_seo = Mock()
        mock_seo.optimize_content.return_value = ("SEO optimized content", {"title": "SEO Title", "description": "SEO Desc", "keywords": ["test"]})

        mock_quality_evaluator = Mock()
        review_result = ReviewResult(
            passed=True,
            score=90,
            issues=[],
            feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value
        )
        mock_quality_evaluator.evaluate.return_value = review_result

        mock_content_fixer = Mock()
        mock_content_fixer.fix.return_value = "Fixed content"

        mock_final_reviewer = Mock()
        mock_final_reviewer.final_review.return_value = "Final reviewed content"

        mock_router = Mock()
        mock_router.decide.return_value = "publish"

        mock_image = Mock()
        mock_image.generate_hero_image.return_value = (None, None)

        mock_learner = Mock()
        mock_learner.analyze_review.return_value = {"學習提案": []}

        agents = {
            "planner": mock_planner,
            "research": mock_research,
            "writer": mock_writer,
            "critic": mock_critic,
            "seo": mock_seo,
            "quality_evaluator": mock_quality_evaluator,
            "content_fixer": mock_content_fixer,
            "final_reviewer": mock_final_reviewer,
            "router": mock_router,
            "image": mock_image,
            "learner": mock_learner,
        }
        
        if frontend_agent:
            agents["frontend"] = frontend_agent
        if publisher:
            agents["publisher"] = publisher
            
        return agents

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_happy_path_frontend_pipeline(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Test happy path: FrontendAgent → Security PASS → Converter PASS → Validator PASS → Publisher reached."""
        task = self._create_test_task(
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH).to_dict(),
        )
        task_id = task.id

        # Mock FrontendAgent
        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        # Mock FrontendSecurityGate
        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        # Mock GreenLightConverter
        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks="<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Test content</div>\n<!-- /wp:greenshift-blocks/element -->",
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        # Mock FrontendValidator
        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate
        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={"inline_css_size": 500},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        # Mock publisher
        mock_publisher = Mock()
        mock_publisher.publish_content.return_value = (123, "https://example.com/post/123")
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                # Attach the mock methods to the class
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertTrue(result)
        task = workflow_state.get_task(task_id)
        # Task goes through COMPLETED then LEARNING; final status is LEARNING
        self.assertIn(task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING])
        self.assertIsNotNone(task.frontend_request)
        self.assertIsNotNone(task.frontend_result)
        self.assertIsNotNone(task.frontend_security_result)
        self.assertIsNotNone(task.frontend_conversion_result)
        self.assertIsNotNone(task.frontend_validation_result)
        mock_publisher.publish_content.assert_called_once()

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_security_failure_stops_pipeline(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Test Security FAIL → retry 3 times → FAILED_NEEDS_ATTENTION, Converter/Validator not called."""
        task = self._create_test_task()
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<script>alert('xss')</script>",
            css="",
            javascript="",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=False,
            html="<script>alert('xss')</script>",
            css="",
            javascript="",
            errors=["Dangerous HTML tag detected: <script>"],
            warnings=[],
            blocked_items=["html:tag:script"],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate (not called since security fails first)
        mock_production = Mock()
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)
        task = workflow_state.get_task(task_id)
        # Now fails after retries exhausted with FAILED_NEEDS_ATTENTION
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.frontend_retry_count, 3)
        self.assertEqual(len(task.frontend_retry_history), 3)
        # Converter and Validator should not be called since security fails first each retry
        mock_converter.convert.assert_not_called()
        mock_validator.validate.assert_not_called()
        mock_publisher.publish_content.assert_not_called()

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_conversion_failure_stops_pipeline(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Test Security PASS → Conversion FAIL → retry 3 times → FAILED_NEEDS_ATTENTION, Validator not called."""
        task = self._create_test_task()
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=False,
            blocks=None,
            errors=["convert.js not found"],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate (not called since conversion fails first)
        mock_production = Mock()
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)
        task = workflow_state.get_task(task_id)
        # Now fails after retries exhausted with FAILED_NEEDS_ATTENTION
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.frontend_retry_count, 3)
        self.assertEqual(len(task.frontend_retry_history), 3)
        # Validator should not be called since conversion fails first each retry
        mock_validator.validate.assert_not_called()
        mock_publisher.publish_content.assert_not_called()

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_validation_failure_stops_pipeline(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Test Security PASS → Conversion PASS → Validation FAIL → retry 3 times → FAILED_NEEDS_ATTENTION."""
        task = self._create_test_task()
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = ;",  # Invalid JS
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = ;",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks="<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Test content</div>\n<!-- /wp:greenshift-blocks/element -->",
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=False,
            validation_status="failed",
            errors=[{"gate": "validation", "type": "js_syntax", "severity": "error", "message": "JS syntax error", "location": {"line": 0, "column": 0}, "context": "Node.js --check reported syntax error"}],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate (not called since validation fails first)
        mock_production = Mock()
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)
        task = workflow_state.get_task(task_id)
        # Now fails after retries exhausted with FAILED_NEEDS_ATTENTION
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.frontend_retry_count, 3)
        self.assertEqual(len(task.frontend_retry_history), 3)
        mock_publisher.publish_content.assert_not_called()
        mock_publisher.publish_content.assert_not_called()


    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_non_interactive_requires_human_review(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class):
        """Test REQUIRE_HUMAN_REVIEW policy pauses at AWAITING_APPROVAL (non-interactive mode simulation)."""
        task = self._create_test_task()
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks="<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Test content</div>\n<!-- /wp:greenshift-blocks/element -->",
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate
        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={"inline_css_size": 500},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)
        task = workflow_state.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        mock_publisher.publish_content.assert_not_called()


    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_interactive_eof_requires_human_review(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class):
        """Test REQUIRE_HUMAN_REVIEW policy pauses at AWAITING_APPROVAL (interactive mode simulation)."""
        task = self._create_test_task()
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks="<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Test content</div>\n<!-- /wp:greenshift-blocks/element -->",
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate
        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={"inline_css_size": 500},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)
        task = workflow_state.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        mock_publisher.publish_content.assert_not_called()


    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_production_quality_fail_then_pass(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Integration test A: Production Quality FAIL → retry → all gates PASS → workflow continues.
        
        Expected:
        - Attempt 1: Security PASS, Converter PASS, Validator PASS, Production Quality FAIL
        - FrontendFailureFeedback created, frontend_retry_count increments, history records Production Quality failure
        - FrontendAgent regenerates with feedback
        - Attempt 2: All four gates PASS
        - Workflow proceeds to AWAITING_APPROVAL (default) or continues (AUTO_PUBLISH)
        """
        task = self._create_test_task(
            final_content="Final content here",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH).to_dict(),
        )
        task_id = task.id

        # Mock FrontendAgent - first call returns HTML with production quality issues, second returns clean
        mock_frontend_agent = Mock()
        call_count = {'frontend': 0}
        
        def generate_frontend_side_effect(*args, **kwargs):
            call_count['frontend'] += 1
            if call_count['frontend'] == 1:
                # First attempt: excessive inline CSS (production quality issue)
                return FrontendResult(
                    task_id=task_id,
                    success=True,
                    html='<style>body {' + 'color: red; ' * 200 + '}</style><div>Test</div>',
                    css="body { color: red; }",
                    javascript="",
                    validation_status="pending",
                )
            else:
                # Second attempt: clean HTML
                return FrontendResult(
                    task_id=task_id,
                    success=True,
                    html='<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->',
                    css="body { color: #333; }",
                    javascript="",
                    validation_status="pending",
                )
        mock_frontend_agent.generate_frontend.side_effect = generate_frontend_side_effect

        # Mock FrontendSecurityGate - always passes
        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html='<style>body {' + 'color: red; ' * 200 + '}</style><div>Test</div>',
            css="body { color: red; }",
            javascript="",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        # Mock GreenLightConverter - always passes
        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks='<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->',
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        # Mock FrontendValidator - always passes
        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate - first fails, second passes
        mock_production = Mock()
        call_count['production'] = 0
        
        def production_check_side_effect(*args, **kwargs):
            call_count['production'] += 1
            if call_count['production'] == 1:
                # First attempt: fails due to excessive inline CSS
                from contracts import FrontendProductionQualityResult
                return FrontendProductionQualityResult(
                    task_id=task_id,
                    passed=False,
                    validation_status="failed",
                    errors=[{"gate": "production_quality", "type": "excessive_inline_css", "severity": "error", "message": "Inline CSS exceeds production budget"}],
                    warnings=[],
                    diagnostics={"inline_css_size": 5000},
                )
            else:
                # Second attempt: passes
                from contracts import FrontendProductionQualityResult
                return FrontendProductionQualityResult(
                    task_id=task_id,
                    passed=True,
                    validation_status="passed",
                    errors=[],
                    warnings=[],
                    diagnostics={"inline_css_size": 500},
                )
        mock_production.check.side_effect = production_check_side_effect
        mock_production_class.return_value = mock_production

        # Mock publisher
        mock_publisher = Mock()
        mock_publisher.publish_content.return_value = (123, "https://example.com/post/123")
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        # Verify final result
        self.assertTrue(result, "Workflow should succeed after retry")

        # Check task state
        final_task = workflow_state.get_task(task_id)
        
        print(f"frontend_retry_count: {final_task.frontend_retry_count}")
        print(f"frontend_retry_history: {final_task.frontend_retry_history}")
        print(f"status: {final_task.status}")
        print(f"frontend_production_quality_result: {final_task.frontend_production_quality_result}")

        # Assertions
        self.assertEqual(final_task.frontend_retry_count, 1, 
            f"Expected frontend_retry_count=1, got {final_task.frontend_retry_count}")
        self.assertEqual(len(final_task.frontend_retry_history), 1,
            f"Expected 1 history entry, got {len(final_task.frontend_retry_history)}")
        
        history_entry = final_task.frontend_retry_history[0]
        self.assertEqual(history_entry['attempt'], 1)
        self.assertEqual(history_entry['gate'], 'FRONTEND_PRODUCTION_QUALITY')
        self.assertEqual(history_entry['action'], 'FRONTEND_REGENERATE')
        self.assertIn('error', history_entry)
        self.assertIn('feedback', history_entry)
        
        # After publishing, workflow goes to LEARNING phase (expected)
        self.assertIn(final_task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING],
            f"Expected COMPLETED or LEARNING, got {final_task.status}")
        self.assertIsNotNone(final_task.frontend_production_quality_result)
        self.assertTrue(final_task.frontend_production_quality_result.get('passed', False))

        print("\nALL ASSERTIONS PASSED")
        print("Production Quality fail -> regenerate -> pass: frontend_retry_count=1, history=1 entry, COMPLETED")


    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_production_quality_retry_exhaustion(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Integration test B: Production Quality repeatedly FAILs → retry exhaustion → FAILED_NEEDS_ATTENTION.
        
        Expected:
        - TaskStatus.FAILED_NEEDS_ATTENTION
        - final_failed_gate == FRONTEND_PRODUCTION_QUALITY
        - Appropriate final error/feedback/history persisted
        """
        task = self._create_test_task()
        task_id = task.id

        # Mock FrontendAgent - always returns HTML with production quality issues
        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html='<style>body {' + 'color: red; ' * 200 + '}</style><div>Test</div>',
            css="body { color: red; }",
            javascript="",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        # Mock FrontendSecurityGate - always passes
        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html='<style>body {' + 'color: red; ' * 200 + '}</style><div>Test</div>',
            css="body { color: red; }",
            javascript="",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        # Mock GreenLightConverter - always passes
        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks='<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->',
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        # Mock FrontendValidator - always passes
        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate - always fails
        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=False,
            validation_status="failed",
            errors=[{"gate": "production_quality", "type": "excessive_inline_css", "severity": "error", "message": "Inline CSS exceeds production budget"}],
            warnings=[],
            diagnostics={"inline_css_size": 5000},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        # Mock publisher
        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)
        task = workflow_state.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.frontend_retry_count, 3)
        self.assertEqual(len(task.frontend_retry_history), 3)
        self.assertEqual(task.final_failed_gate, "FRONTEND_PRODUCTION_QUALITY")
        self.assertIsNotNone(task.final_error)
        self.assertIsNotNone(task.final_feedback)
        self.assertIsNotNone(task.failure_timestamp)
        
        # Verify all history entries are for Production Quality
        for entry in task.frontend_retry_history:
            self.assertEqual(entry['gate'], 'FRONTEND_PRODUCTION_QUALITY')
            self.assertEqual(entry['action'], 'FRONTEND_REGENERATE')
        
        mock_publisher.publish_content.assert_not_called()
        print("\nALL ASSERTIONS PASSED")
        print("Production Quality retry exhaustion: FAILED_NEEDS_ATTENTION, final_failed_gate=FRONTEND_PRODUCTION_QUALITY")


    # Phase 7C-4: Approval Policy Tests
    
    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_auto_publish_flow(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Test AUTO_PUBLISH: All gates PASS → AUTO_PUBLISH → ImageAgent → Publisher → COMPLETED."""
        task = self._create_test_task(
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH).to_dict(),
        )
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks="<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->",
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={"inline_css_size": 500},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.publish_content.return_value = (123, "https://example.com/post/123")
        mock_publisher.validate_config.return_value = True

        mock_image = Mock()
        mock_image.generate_hero_image.return_value = (None, None)

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)
        agents["image"] = mock_image

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertTrue(result)
        task = workflow_state.get_task(task_id)
        self.assertIn(task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING])
        self.assertEqual(task.approval_policy["mode"], "auto_publish")
        self.assertIsNotNone(task.wordpress_id)
        mock_publisher.publish_content.assert_called_once()

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_require_human_review_pauses_at_awaiting_approval(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Test REQUIRE_HUMAN_REVIEW (default): All gates PASS → REQUIRE_HUMAN_REVIEW → AWAITING_APPROVAL."""
        task = self._create_test_task()  # No explicit policy = default REQUIRE_HUMAN_REVIEW
        task_id = task.id

        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="const x = 1;",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks="<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->",
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={"inline_css_size": 500},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertFalse(result)  # Paused, not failed
        task = workflow_state.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        self.assertIsNotNone(task.approval_requested_at)
        self.assertEqual(task.approval_policy["mode"], "require_human_review")
        mock_publisher.publish_content.assert_not_called()

    def test_approval_resume_approved(self):
        """Test AWAITING_APPROVAL + approved=True → resumes workflow → COMPLETED."""
        task = self._create_test_task(
            status=TaskStatus.AWAITING_APPROVAL,
            approval_status="pending",
            approval_requested_at="2024-01-01T00:00:00",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict(),
            final_content="Final content here",
        )
        task_id = task.id

        mock_publisher = Mock()
        mock_publisher.publish_content.return_value = (123, "https://example.com/post/123")
        mock_publisher.validate_config.return_value = True

        mock_image = Mock()
        mock_image.generate_hero_image.return_value = (None, None)

        mock_learner = Mock()
        mock_learner.analyze_review.return_value = {"學習提案": []}

        agents = {"publisher": mock_publisher, "image": mock_image, "learner": mock_learner}

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.submit_approval_decision(task_id, approved=True, feedback="Looks good")

        self.assertTrue(result)
        task = workflow_state.get_task(task_id)
        self.assertEqual(task.approval_status, "approved")
        self.assertTrue(task.approval_decision)
        self.assertEqual(task.approval_feedback, "Looks good")
        self.assertIn(task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING])
        mock_publisher.publish_content.assert_called_once()

    def test_approval_resume_rejected(self):
        """Test AWAITING_APPROVAL + approved=False → REJECTED_NEEDS_REVISION → STOP."""
        task = self._create_test_task(
            status=TaskStatus.AWAITING_APPROVAL,
            approval_status="pending",
            approval_requested_at="2024-01-01T00:00:00",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict(),
            final_content="Final content here",
        )
        task_id = task.id

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True

        agents = {"publisher": mock_publisher}

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.submit_approval_decision(task_id, approved=False, feedback="Needs more detail")

        self.assertTrue(result)
        task = workflow_state.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.REJECTED_NEEDS_REVISION)
        self.assertEqual(task.approval_status, "rejected")
        self.assertFalse(task.approval_decision)
        self.assertEqual(task.approval_feedback, "Needs more detail")
        mock_publisher.publish_content.assert_not_called()

    def test_approval_rejection_no_frontend_failure_feedback(self):
        """Test human rejection does NOT create FrontendFailureFeedback or increment frontend_retry_count."""
        task = self._create_test_task(
            status=TaskStatus.AWAITING_APPROVAL,
            approval_status="pending",
            approval_requested_at="2024-01-01T00:00:00",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict(),
            frontend_retry_count=0,
            frontend_retry_history=[],
        )
        task_id = task.id

        mock_publisher = Mock()
        mock_publisher.validate_config.return_value = True
        agents = {"publisher": mock_publisher}

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            self.factory.submit_approval_decision(task_id, approved=False, feedback="Design not approved")

        task = workflow_state.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.REJECTED_NEEDS_REVISION)
        self.assertEqual(task.frontend_retry_count, 0)  # Not incremented
        self.assertEqual(len(task.frontend_retry_history), 0)  # No frontend retry recorded

    def test_approval_persistence_roundtrip(self):
        """Test AWAITING_APPROVAL and REJECTED_NEEDS_REVISION persist and restore correctly."""
        task = self._create_test_task(
            status=TaskStatus.AWAITING_APPROVAL,
            approval_status="pending",
            approval_requested_at="2024-01-01T00:00:00",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict(),
            final_content="Final content",
        )
        task_id = task.id
        
        # Save and load
        self.factory.save_state("test_approval_state.json")
        
        # Create new factory and load state
        new_factory = AIWordPressFactory()
        new_factory.load_state("test_approval_state.json")
        
        loaded_task = workflow_state.get_task(task_id)
        self.assertEqual(loaded_task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(loaded_task.approval_status, "pending")
        self.assertEqual(loaded_task.approval_policy["mode"], "require_human_review")
        
        # Test REJECTED_NEEDS_REVISION persistence
        task2 = self._create_test_task(
            status=TaskStatus.REJECTED_NEEDS_REVISION,
            approval_status="rejected",
            approval_requested_at="2024-01-01T00:00:00",
            approval_decided_at="2024-01-02T00:00:00",
            approval_feedback="Needs revision",
            approval_decision=False,
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict(),
            final_content="Final content",
        )
        task2_id = task2.id
        
        self.factory.save_state("test_approval_state2.json")
        
        new_factory2 = AIWordPressFactory()
        new_factory2.load_state("test_approval_state2.json")
        
        loaded_task2 = workflow_state.get_task(task2_id)
        self.assertEqual(loaded_task2.status, TaskStatus.REJECTED_NEEDS_REVISION)
        self.assertEqual(loaded_task2.approval_status, "rejected")
        self.assertFalse(loaded_task2.approval_decision)
        self.assertEqual(loaded_task2.approval_feedback, "Needs revision")

    # ==================== PHASE 7C-4A BRAND RULES INTEGRATION TESTS ====================

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_brand_rules_pass_full_pipeline(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Integration test: ClientProfile → BrandProfile → BrandProductionRules → Production Gate → AUTO_PUBLISH.
        
        Brand PASS scenario: all brand rules satisfied.
        """
        # Create task with client profile containing brand profile
        brand_rules = BrandProductionRules(
            allowed_font_families=["Noto Sans TC"],
            allowed_colors=["#2C2420", "#FFFFFF"],
            max_content_width_px=1200,
            allowed_border_radius_px=[4, 8],
        )
        brand_profile = BrandProfile(
            brand_id="brand-123",
            display_name="Test Brand",
            production_rules=brand_rules,
        )
        client_profile = ClientProfile(
            client_id="client-456",
            display_name="Test Client",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH),
            brand_profile=brand_profile,
        )
        
        task = self._create_test_task(
            client_profile=client_profile.to_dict(),
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH).to_dict(),
        )
        task_id = task.id

        # Mock FrontendAgent - returns brand-compliant output
        mock_frontend_agent = Mock()
        frontend_result = FrontendResult(
            task_id=task_id,
            success=True,
            html='<div style="font-family: Noto Sans TC; color: #2C2420; max-width: 1000px; border-radius: 8px;">Test</div>',
            css='body { font-family: "Noto Sans TC"; color: #2C2420; max-width: 1000px; border-radius: 8px; }',
            javascript="",
            validation_status="pending",
        )
        mock_frontend_agent.generate_frontend.return_value = frontend_result

        # Mock FrontendSecurityGate - always passes
        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html=frontend_result.html,
            css=frontend_result.css,
            javascript="",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        # Mock GreenLightConverter - always passes
        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks='<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->',
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        # Mock FrontendValidator - always passes
        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate - passes with brand rules
        mock_production = Mock()
        from contracts import FrontendProductionQualityResult
        production_result = FrontendProductionQualityResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={"brand_rules_evaluated": True},
        )
        mock_production.check.return_value = production_result
        mock_production_class.return_value = mock_production

        # Mock publisher and image
        mock_publisher = Mock()
        mock_publisher.publish_content.return_value = (123, "https://example.com/post/123")
        mock_publisher.validate_config.return_value = True

        mock_image = Mock()
        mock_image.generate_hero_image.return_value = (None, None)

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)
        agents["image"] = mock_image

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        self.assertTrue(result, "Workflow should succeed with brand-compliant output")
        task = workflow_state.get_task(task_id)
        self.assertIn(task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING])
        mock_publisher.publish_content.assert_called_once()

    @patch('builtins.input', return_value='.')
    @patch('main.FrontendSecurityGate')
    @patch('main.GreenLightConverter')
    @patch('main.FrontendValidator')
    @patch('main.FrontendProductionQualityGate')
    def test_brand_font_error_triggers_retry(self, mock_production_class, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Integration test: Brand ERROR → Production Gate FAIL → retry → regenerate → PASS.
        
        Brand font violation should trigger existing frontend retry mechanism.
        """
        # Create task with brand profile that only allows Noto Sans TC
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        brand_profile = BrandProfile(
            brand_id="brand-123",
            display_name="Test Brand",
            production_rules=brand_rules,
        )
        client_profile = ClientProfile(
            client_id="client-456",
            display_name="Test Client",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH),
            brand_profile=brand_profile,
        )
        
        task = self._create_test_task(
            client_profile=client_profile.to_dict(),
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH).to_dict(),
        )
        task_id = task.id

        # Mock FrontendAgent - first call uses disallowed font, second uses allowed
        mock_frontend_agent = Mock()
        call_count = {'frontend': 0}
        
        def generate_frontend_side_effect(*args, **kwargs):
            call_count['frontend'] += 1
            if call_count['frontend'] == 1:
                # First attempt: uses Arial (not allowed by brand)
                return FrontendResult(
                    task_id=task_id,
                    success=True,
                    html='<div style="font-family: Arial, sans-serif;">Test</div>',
                    css='body { font-family: Arial, sans-serif; }',
                    javascript="",
                    validation_status="pending",
                )
            else:
                # Second attempt: uses Noto Sans TC (allowed by brand)
                return FrontendResult(
                    task_id=task_id,
                    success=True,
                    html='<div style="font-family: Noto Sans TC, sans-serif;">Test</div>',
                    css='body { font-family: "Noto Sans TC", sans-serif; }',
                    javascript="",
                    validation_status="pending",
                )
        mock_frontend_agent.generate_frontend.side_effect = generate_frontend_side_effect

        # Mock FrontendSecurityGate - always passes
        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html='<div style="font-family: Arial, sans-serif;">Test</div>',
            css='body { font-family: Arial, sans-serif; }',
            javascript="",
            errors=[],
            warnings=[],
            blocked_items=[],
        )
        mock_security.check.return_value = security_result
        mock_security_class.return_value = mock_security

        # Mock GreenLightConverter - always passes
        mock_converter = Mock()
        conversion_result = GreenLightConversionResult(
            task_id=task_id,
            success=True,
            blocks='<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->',
            errors=[],
            warnings=[],
        )
        mock_converter.convert.return_value = conversion_result
        mock_converter_class.return_value = mock_converter

        # Mock FrontendValidator - always passes
        mock_validator = Mock()
        validation_result = FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
            validator_error=False,
        )
        mock_validator.validate.return_value = validation_result
        mock_validator_class.return_value = mock_validator

        # Mock FrontendProductionQualityGate - first fails on brand font, second passes
        mock_production = Mock()
        call_count['production'] = 0
        
        def production_check_side_effect(*args, **kwargs):
            call_count['production'] += 1
            if call_count['production'] == 1:
                # First attempt: fails due to brand font violation
                from contracts import FrontendProductionQualityResult
                return FrontendProductionQualityResult(
                    task_id=task_id,
                    passed=False,
                    validation_status="failed",
                    errors=[{"gate": "production_quality", "type": "brand_font_family_not_allowed", "severity": "error", "message": "Primary font Arial not allowed by brand profile", "location": {"line": 0, "column": 0}, "context": "Allowed: Noto Sans TC", "rule_source": "brand"}],
                    warnings=[],
                    diagnostics={"brand_rules_evaluated": True, "brand_font_violations": [{"primary_font": "Arial", "declaration": "Arial, sans-serif"}]},
                )
            else:
                # Second attempt: passes
                from contracts import FrontendProductionQualityResult
                return FrontendProductionQualityResult(
                    task_id=task_id,
                    passed=True,
                    validation_status="passed",
                    errors=[],
                    warnings=[],
                    diagnostics={"brand_rules_evaluated": True},
                )
        mock_production.check.side_effect = production_check_side_effect
        mock_production_class.return_value = mock_production

        # Mock publisher and image
        mock_publisher = Mock()
        mock_publisher.publish_content.return_value = (123, "https://example.com/post/123")
        mock_publisher.validate_config.return_value = True

        mock_image = Mock()
        mock_image.generate_hero_image.return_value = (None, None)

        agents = self._create_mock_agents(frontend_agent=mock_frontend_agent, publisher=mock_publisher)
        agents["image"] = mock_image

        def get_agent_side_effect(agent_type):
            agent_instance = agents.get(agent_type)
            if agent_instance:
                class MockClass:
                    def __init__(self, config):
                        pass
                for attr in dir(agent_instance):
                    if not attr.startswith('_'):
                        setattr(MockClass, attr, getattr(agent_instance, attr))
                return MockClass
            return None

        with patch.object(self.factory, '_get_agent', side_effect=get_agent_side_effect):
            result = self.factory.run_workflow(task_id)

        # Verify final result
        self.assertTrue(result, "Workflow should succeed after retry")

        # Check task state
        final_task = workflow_state.get_task(task_id)
        
        # Assertions
        self.assertEqual(final_task.frontend_retry_count, 1, 
            f"Expected frontend_retry_count=1, got {final_task.frontend_retry_count}")
        self.assertEqual(len(final_task.frontend_retry_history), 1,
            f"Expected 1 history entry, got {len(final_task.frontend_retry_history)}")
        
        history_entry = final_task.frontend_retry_history[0]
        self.assertEqual(history_entry['attempt'], 1)
        self.assertEqual(history_entry['gate'], 'FRONTEND_PRODUCTION_QUALITY')
        self.assertEqual(history_entry['action'], 'FRONTEND_REGENERATE')
        
        # After publishing, workflow goes to LEARNING phase
        self.assertIn(final_task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING],
            f"Expected COMPLETED or LEARNING, got {final_task.status}")
        self.assertIsNotNone(final_task.frontend_production_quality_result)
        self.assertTrue(final_task.frontend_production_quality_result.get('passed', False))

        print("\nALL ASSERTIONS PASSED")
        print("Brand font ERROR -> regenerate -> pass: frontend_retry_count=1, history=1 entry, COMPLETED")


if __name__ == "__main__":
    unittest.main()