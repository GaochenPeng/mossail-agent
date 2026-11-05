import pytest
from agent_runtime.tools import calculate_expression


def test_arithmetic_tool():
    assert calculate_expression('(17+25)*3') == 126
    assert calculate_expression('126/3') == 42


@pytest.mark.parametrize('expression', ['__import__("os").system("id")', '(1).__class__', '2**10000', '[1,2]', '9'*201])
def test_tool_rejects_code_execution_and_excessive_input(expression):
    with pytest.raises((ValueError, SyntaxError)):
        calculate_expression(expression)
