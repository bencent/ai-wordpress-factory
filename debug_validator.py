from tools.frontend_validator import FrontendValidator, _HTMLValidationParser

parser = _HTMLValidationParser()
html = '<div class="hello">Hello</div>'
parser.set_html(html)
parser.feed(html)
print("Errors:", parser.get_errors())
print("Tag stack:", parser.tag_stack)