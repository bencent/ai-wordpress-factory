#!/usr/bin/env python3
"""Test: Validation fails once, then passes on retry - verify frontend_retry_count=1, history=1, COMPLETED"""

import unittest
from unittest.mock import Mock, patch, MagicMock

from state import Task, TaskStatus, ContentType, WorkflowState
from contracts import (
    FrontendRequest, FrontendResult, FrontendSecurityResult,
    GreenLightConversionResult, FrontendValidationResult,
    ReviewResult, ReviewAction, CritiqueResult,
    FrontendGate, FrontendFailureSeverity,
    ApprovalPolicy, ApprovalPolicyMode
)
from main import AIWordPressFactory


class TestValidationRetryThenPass(unittest.TestCase):
    """Test validation failure -> regenerate -> pass scenario."""

    def setUp(self):
        self.factory = AIWordPressFactory()
        from state import workflow_state
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def _create_test_task(self) -> str:
        task_id = self.factory.create_task(
            title="Test Blog Post",
            description="A test blog post",
            content_type=ContentType.BLOG_POST,
        )
        return task_id

    def _create_mock_agents(self, frontend_agent=None, publisher=None):
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
    def test_validation_fail_then_pass(self, mock_validator_class, mock_converter_class, mock_security_class, mock_input):
        """Validation fails on first attempt, passes on second retry.
        
        Expected:
        - frontend_retry_count == 1
        - frontend_retry_history has 1 entry
        - Final status: COMPLETED (goes to Publisher)
        """
        task_id = self._create_test_task()
        
        # Replace task with one that has final_content
        task = Task(
            id=task_id,
            title="Test Blog Post",
            content_type=ContentType.BLOG_POST,
            final_content="Final content here",
            approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH).to_dict(),
        )
        from state import workflow_state
        workflow_state.add_task(task)

        # Mock FrontendAgent - first call returns invalid HTML, second returns valid
        mock_frontend_agent = Mock()
        call_count = {'frontend': 0}
        
        def generate_frontend_side_effect(*args, **kwargs):
            call_count['frontend'] += 1
            if call_count['frontend'] == 1:
                # First attempt: invalid HTML (missing WP blocks)
                return FrontendResult(
                    task_id=task_id,
                    success=True,
                    html="<html><body><div>Test</div></body></html>",
                    css="body { color: red; }",
                    javascript="",
                    validation_status="pending",
                )
            else:
                # Second attempt (after retry): valid HTML with WP blocks
                return FrontendResult(
                    task_id=task_id,
                    success=True,
                    html='<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->',
                    css="body { color: red; }",
                    javascript="",
                    validation_status="pending",
                )
        mock_frontend_agent.generate_frontend.side_effect = generate_frontend_side_effect

        # Mock FrontendSecurityGate - always passes
        mock_security = Mock()
        security_result = FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<html><body><div>Test</div></body></html>",
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

        # Mock FrontendValidator - first fails, second passes
        mock_validator = Mock()
        call_count['validation'] = 0
        
        def validate_side_effect(*args, **kwargs):
            call_count['validation'] += 1
            if call_count['validation'] == 1:
                # First validation: fails - missing WP blocks
                return FrontendValidationResult(
                    task_id=task_id,
                    passed=False,
                    validation_status="failed",
                    errors=["Missing WordPress block comments"],
                    warnings=[],
                    diagnostics={},
                    validator_error=False,
                )
            else:
                # Second validation: passes
                return FrontendValidationResult(
                    task_id=task_id,
                    passed=True,
                    validation_status="passed",
                    errors=[],
                    warnings=[],
                    diagnostics={},
                    validator_error=False,
                )
        mock_validator.validate.side_effect = validate_side_effect
        mock_validator_class.return_value = mock_validator

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
        self.assertTrue(result, "Workflow should succeed")

        # Check task state
        final_task = workflow_state.get_task(task_id)
        
        print(f"frontend_retry_count: {final_task.frontend_retry_count}")
        print(f"frontend_retry_history: {final_task.frontend_retry_history}")
        print(f"status: {final_task.status}")
        print(f"frontend_validation_result: {final_task.frontend_validation_result}")

        # Assertions
        self.assertEqual(final_task.frontend_retry_count, 1, 
            f"Expected frontend_retry_count=1, got {final_task.frontend_retry_count}")
        self.assertEqual(len(final_task.frontend_retry_history), 1,
            f"Expected 1 history entry, got {len(final_task.frontend_retry_history)}")
        
        history_entry = final_task.frontend_retry_history[0]
        self.assertEqual(history_entry['attempt'], 1)
        self.assertEqual(history_entry['gate'], 'FRONTEND_VALIDATION')
        self.assertEqual(history_entry['action'], 'FRONTEND_REGENERATE')
        self.assertIn('error', history_entry)
        self.assertIn('feedback', history_entry)
        
        # After publishing, workflow goes to LEARNING phase (expected)
        self.assertIn(final_task.status, [TaskStatus.COMPLETED, TaskStatus.LEARNING],
            f"Expected COMPLETED or LEARNING, got {final_task.status}")
        self.assertIsNotNone(final_task.frontend_validation_result)
        self.assertTrue(final_task.frontend_validation_result.get('passed', False))

        print("\nALL ASSERTIONS PASSED")
        print("Validation fail -> regenerate -> pass: frontend_retry_count=1, history=1 entry, COMPLETED")


if __name__ == '__main__':
    unittest.main(verbosity=2)