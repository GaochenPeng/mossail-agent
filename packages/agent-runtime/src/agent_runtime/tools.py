"""首个真实工具；不执行任意代码，不依赖云 sandbox。"""
import ast
import math
import operator

from agents import function_tool

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def calculate_expression(expression: str) -> float:
    if len(expression) > 200:
        raise ValueError('expression too long')
    tree = ast.parse(expression, mode='eval')
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError('expression too complex')

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            value = _OPS[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            value = _OPS[type(node.op)](visit(node.operand))
        else:
            raise ValueError('only numbers and + - * / are supported')
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError('result out of range')
        return value

    return float(visit(tree.body))


@function_tool
def calculate(expression: str) -> str:
    """Calculate a numeric expression using +, -, *, / and parentheses.

    Args:
        expression: The arithmetic expression to evaluate.
    """
    return str(calculate_expression(expression))
