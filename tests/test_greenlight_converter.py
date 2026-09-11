#!/usr/bin/env python3
"""Tests for Phase 7B-2A simplified GreenLightConverter tool."""

import os
import subprocess
from unittest.mock import patch, MagicMock

from contracts import GreenLightConversionResult
from tools.greenlight_converter import GreenLightConverter


class MockConfig:
    pass


def test_greenlight_converter_instantiation():
    converter = GreenLightConverter(MockConfig())
    assert converter.name == "greenlightconverter"
    assert "HTML" in converter.description or "HTML/CSS/JS" in converter.description


def test_convert_script_not_found():
    converter = GreenLightConverter(MockConfig())
    with patch.object(converter, '_resolve_script', return_value='/nonexistent/convert.js'):
        result = converter.convert('<div>test</div>')
    assert result.success is False
    assert result.blocks is None
    assert 'convert.js not found' in result.errors[0]


def test_deconvert_script_not_found():
    converter = GreenLightConverter(MockConfig())
    with patch.object(converter, '_resolve_script', return_value='/nonexistent/deconvert.js'):
        result = converter.deconvert('<!-- wp:paragraph -->test<!-- /wp:paragraph -->')
    assert result.success is False
    assert result.blocks is None
    assert 'deconvert.js not found' in result.errors[0]


def test_convert_success():
    converter = GreenLightConverter(MockConfig())
    fake_output = '<!-- wp:paragraph -->\n<p>Hello</p>\n<!-- /wp:paragraph -->\n'
    mock_result = subprocess.CompletedProcess(
        args=['node', 'convert.js'],
        returncode=0,
        stdout=fake_output,
        stderr='',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result) as mock_run:
        result = converter.convert('<div>Hello</div>')
    mock_run.assert_called_once()
    assert result.success is True
    assert result.blocks == fake_output


def test_convert_failure():
    converter = GreenLightConverter(MockConfig())
    mock_result = subprocess.CompletedProcess(
        args=['node', 'convert.js'],
        returncode=1,
        stdout='',
        stderr='Parse error',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result):
        result = converter.convert('<div>Hello</div>')
    assert result.success is False
    assert result.errors == ['Parse error']


def test_deconvert_success():
    converter = GreenLightConverter(MockConfig())
    fake_output = '<!DOCTYPE html>\n<html>\n<body>Hello</body>\n</html>\n'
    mock_result = subprocess.CompletedProcess(
        args=['node', 'deconvert.js'],
        returncode=0,
        stdout=fake_output,
        stderr='',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result) as mock_run:
        result = converter.deconvert('<!-- wp:paragraph -->\n<p>Hello</p>\n<!-- /wp:paragraph -->')
    mock_run.assert_called_once()
    assert result.success is True
    assert result.blocks == fake_output


def test_convert_resolve_script_path():
    converter = GreenLightConverter(MockConfig())
    script_path = converter._resolve_script('convert.js')
    assert script_path.endswith('convert.js')
    assert 'greenlight-vibe' in script_path
    assert 'scripts' in script_path


def test_deconvert_resolve_script_path():
    converter = GreenLightConverter(MockConfig())
    script_path = converter._resolve_script('deconvert.js')
    assert script_path.endswith('deconvert.js')
    assert 'greenlight-vibe' in script_path
    assert 'scripts' in script_path


def test_convert_output_integrity_no_strip():
    converter = GreenLightConverter(MockConfig())
    fake_output = '\n\n  <!-- wp:paragraph -->\n<p>Hello</p>\n<!-- /wp:paragraph -->\n\n  \n'
    mock_result = subprocess.CompletedProcess(
        args=['node', 'convert.js'],
        returncode=0,
        stdout=fake_output,
        stderr='',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result):
        result = converter.convert('<div>Hello</div>')
    assert result.success is True
    assert result.blocks == fake_output
    assert result.blocks.startswith('\n\n  ')
    assert result.blocks.endswith('\n\n  \n')


def test_convert_timeout():
    converter = GreenLightConverter(MockConfig())
    with patch('tools.greenlight_converter.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='node', timeout=120)):
        result = converter.convert('<div>Hello</div>')
    assert result.success is False
    assert 'timed out after 120 seconds' in result.errors[0]


def test_convert_node_not_found():
    converter = GreenLightConverter(MockConfig())
    with patch('tools.greenlight_converter.subprocess.run', side_effect=FileNotFoundError):
        result = converter.convert('<div>Hello</div>')
    assert result.success is False
    assert 'Node.js executable not found' in result.errors[0]


def test_convert_nonzero_exit_with_stderr():
    converter = GreenLightConverter(MockConfig())
    mock_result = subprocess.CompletedProcess(
        args=['node', 'convert.js'],
        returncode=2,
        stdout='',
        stderr='Unexpected token <',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result):
        result = converter.convert('<div>Hello</div>')
    assert result.success is False
    assert result.errors == ['Unexpected token <']


def test_convert_success_with_stderr_warning():
    converter = GreenLightConverter(MockConfig())
    fake_output = '<!-- wp:paragraph -->\n<p>Hello</p>\n<!-- /wp:paragraph -->'
    mock_result = subprocess.CompletedProcess(
        args=['node', 'convert.js'],
        returncode=0,
        stdout=fake_output,
        stderr='Deprecated feature used',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result):
        result = converter.convert('<div>Hello</div>')
    assert result.success is True
    assert result.blocks == fake_output
    assert result.warnings == ['Deprecated feature used']


def test_deconvert_output_integrity_no_strip():
    converter = GreenLightConverter(MockConfig())
    fake_output = '\n\n  <!DOCTYPE html>\n<html>\n<body>Hello</body>\n</html>\n\n  \n'
    mock_result = subprocess.CompletedProcess(
        args=['node', 'deconvert.js'],
        returncode=0,
        stdout=fake_output,
        stderr='',
    )
    with patch('tools.greenlight_converter.subprocess.run', return_value=mock_result):
        result = converter.deconvert('<!-- wp:paragraph -->\n<p>Hello</p>\n<!-- /wp:paragraph -->')
    assert result.success is True
    assert result.blocks == fake_output
    assert result.blocks.startswith('\n\n  ')
    assert result.blocks.endswith('\n\n  \n')


def test_no_tempfile_import():
    import tools.greenlight_converter as module
    assert not hasattr(module, 'tempfile'), 'tempfile module should not be imported'


if __name__ == '__main__':
    test_greenlight_converter_instantiation()
    test_convert_script_not_found()
    test_deconvert_script_not_found()
    test_convert_success()
    test_convert_failure()
    test_deconvert_success()
    test_convert_resolve_script_path()
    test_deconvert_resolve_script_path()
    test_convert_output_integrity_no_strip()
    test_convert_timeout()
    test_convert_node_not_found()
    test_convert_nonzero_exit_with_stderr()
    test_convert_success_with_stderr_warning()
    test_deconvert_output_integrity_no_strip()
    test_no_tempfile_import()
    print('All GreenLightConverter tests passed.')
