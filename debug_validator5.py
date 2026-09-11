from contracts import FrontendResult
from tools.frontend_validator import FrontendValidator

validator = FrontendValidator(None)

# Test parser failure handling
print("=== Test parser failure handling ===")
result = FrontendResult(
    task_id="test",
    success=True,
    html="<",
    css="body { color: #333; }",
    javascript="const x = 1;",
    blocks='<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)