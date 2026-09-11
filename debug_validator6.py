from contracts import FrontendResult
from tools.frontend_validator import FrontendValidator

validator = FrontendValidator(None)

# Test valid CSS
print("=== Test valid CSS ===")
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="body { color: #333; }",
    javascript="const x = 1;",
    blocks='<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)
print("Warnings:", vr.warnings)

# Test empty CSS
print("\n=== Test empty CSS ===")
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="",
    javascript="const x = 1;",
    blocks='<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)
print("Warnings:", vr.warnings)

# Test valid JS
print("\n=== Test valid JS ===")
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="body { color: #333; }",
    javascript="const x = 1;",
    blocks='<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)

# Test empty JS
print("\n=== Test empty JS ===")
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="body { color: #333; }",
    javascript="",
    blocks='<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)

# Test valid blocks
print("\n=== Test valid blocks ===")
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="body { color: #333; }",
    javascript="const x = 1;",
    blocks='<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)

# Test multiple blocks
print("\n=== Test multiple blocks ===")
blocks = (
    '<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hello</div>\n<!-- /wp:greenshift-blocks/element -->\n'
    '<!-- wp:greenshift-blocks/element {"tag":"p"} -->\n<p>World</p>\n<!-- /wp:greenshift-blocks/element -->'
)
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="body { color: #333; }",
    javascript="const x = 1;",
    blocks=blocks
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)

# Test style manager block
print("\n=== Test style manager block ===")
blocks3 = (
    '<!-- wp:greenshift-blocks/element {"tag":"div","type":"no","isVariation":"stylemanager"} -->\n'
    '<div class="my-class"></div>\n'
    '<!-- /wp:greenshift-blocks/element -->'
)
result = FrontendResult(
    task_id="test",
    success=True,
    html="<div>Hello</div>",
    css="body { color: #333; }",
    javascript="const x = 1;",
    blocks=blocks3
)
vr = validator.validate(result)
print("Passed:", vr.passed)
print("Status:", vr.validation_status)
print("Errors:", vr.errors)