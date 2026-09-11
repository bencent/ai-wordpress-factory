from contracts import FrontendResult
from tools.frontend_validator import FrontendValidator

class MockConfig:
    pass

def make_validator(config=None):
    if config is None:
        config = MockConfig()
    return FrontendValidator(config)

def make_result(html="", css="", javascript="", blocks=None, task_id="test"):
    if blocks is None:
        blocks = '<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Content</div>\n<!-- /wp:greenshift-blocks/element -->'
    return FrontendResult(
        task_id=task_id,
        success=True,
        html=html,
        css=css,
        javascript=javascript,
        blocks=blocks,
    )

# Test valid_css - exactly as in test
validator = make_validator()
result = make_result(css="body { color: #333; }")
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)
print("Warnings:", vr.warnings)