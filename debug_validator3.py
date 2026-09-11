from tools.frontend_validator import FrontendValidator, _HTMLValidationParser
from contracts import FrontendResult

# Test CSS validation
validator = FrontendValidator(None)

# Test 1: Valid CSS
css = "body { color: #333; margin: 10px; }"
print("Testing CSS:", css)
errors, warnings = validator._validate_css(css)
print("Errors:", errors)
print("Warnings:", warnings)

# Test 2: Empty CSS
print("\n--- Empty CSS ---")
errors, warnings = validator._validate_css("")
print("Errors:", errors)
print("Warnings:", warnings)

# Test 3: Blocks validation
blocks = '<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->'
print("\n--- Blocks ---")
block_errors, block_warnings = validator._validate_blocks(blocks)
print("Block Errors:", block_errors)
print("Block Warnings:", block_warnings)

# Test 4: Multiple blocks
blocks2 = (
    '<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Hello</div>\n<!-- /wp:greenshift-blocks/element -->\n'
    '<!-- wp:greenshift-blocks/element {"tag":"p"} -->\n<p>World</p>\n<!-- /wp:greenshift-blocks/element -->'
)
print("\n--- Multiple Blocks ---")
block_errors, block_warnings = validator._validate_blocks(blocks2)
print("Block Errors:", block_errors)
print("Block Warnings:", block_warnings)

# Test 5: Style manager block
blocks3 = (
    '<!-- wp:greenshift-blocks/element {"tag":"div","type":"no","isVariation":"stylemanager"} -->\n'
    '<div class="my-class"></div>\n'
    '<!-- /wp:greenshift-blocks/element -->'
)
print("\n--- Style Manager Block ---")
block_errors, block_warnings = validator._validate_blocks(blocks3)
print("Block Errors:", block_errors)
print("Block Warnings:", block_warnings)