"""calculate 工具：确定性的算术、换算与日期推算，供模型核对数字而不是心算。

只解释白名单内的 AST 节点：数字、四则与幂运算、比较、少量数学函数和日期函数。
没有名字查找、属性访问和任意调用，模型传什么表达式都碰不到解释器本身；
整数位数与幂指数有上限，防止 9**9**9 这类表达式把进程算死。
"""

import ast
import re
import math
import operator
from datetime import date, datetime, timedelta

from . import config
from .i18n import phrases

MAX_EXPRESSION_CHARS = 500
_MAX_INT_BITS = 10_000  # 约 3000 位十进制
_MAX_FACTORIAL = 1000

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_COMPARES = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}


class CalcError(ValueError):
    pass


def _number(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise CalcError("expected a number")
    return x


def _to_date(x) -> date:
    if isinstance(x, date):
        return x
    if isinstance(x, str):
        try:
            return date.fromisoformat(x.strip())
        except ValueError:
            raise CalcError(f"bad date {x!r}, use YYYY-MM-DD") from None
    raise CalcError("expected a date")


def _factorial(n):
    n = _number(n)
    if not isinstance(n, int) or not 0 <= n <= _MAX_FACTORIAL:
        raise CalcError(f"factorial needs an integer in 0..{_MAX_FACTORIAL}")
    return math.factorial(n)


def _log(x, base=None):
    return math.log(_number(x)) if base is None else math.log(_number(x), _number(base))


def _weekday(x) -> str:
    return phrases("weekday")[_to_date(x).weekday()]


_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(_number(x)) ** (1 / 3), x),
    "exp": math.exp, "log": _log, "log10": math.log10, "log2": math.log2,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "radians": math.radians, "degrees": math.degrees,
    "floor": math.floor, "ceil": math.ceil, "factorial": _factorial,
    "gcd": math.gcd, "lcm": math.lcm,
    "date": _to_date, "days": lambda n: timedelta(days=_number(n)), "weekday": _weekday,
    "today": lambda: datetime.now(config.BOT_TZ).date(),
}
# 只有这些函数接受字符串参数（日期字面量）
_STR_ARG_FUNCS = {"date", "weekday"}


def _check_size(x):
    if isinstance(x, int) and not isinstance(x, bool) and x.bit_length() > _MAX_INT_BITS:
        raise CalcError("result too large")
    return x


def _pow(a, b):
    a, b = _number(a), _number(b)
    if isinstance(a, int) and isinstance(b, int) and b > 0:
        # 先估算结果位数再算，避免真的去算一个天文数字
        if a not in (0, 1, -1) and (abs(a).bit_length() - 1) * b > _MAX_INT_BITS:
            raise CalcError("result too large")
    return a ** b


def _eval(node, env: dict):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("only numbers are allowed here")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalcError(f"unknown name {node.id!r}")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_operand(_eval(node.operand, env)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Pow):
            return _check_size(_pow(left, right))
        return _check_size(_BINOPS[type(node.op)](_operand(left), _operand(right)))
    if isinstance(node, ast.Compare) and all(type(op) in _COMPARES for op in node.ops):
        left = _eval(node.left, env)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, env)
            if not _COMPARES[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        name = node.func.id
        if name not in _FUNCS:
            raise CalcError(f"unknown function {name!r}")
        args = []
        for arg in node.args:
            if (name in _STR_ARG_FUNCS and isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)):
                args.append(arg.value)
            else:
                args.append(_eval(arg, env))
        return _check_size(_FUNCS[name](*args))
    raise CalcError(f"unsupported syntax: {type(node).__name__}")


def _operand(x):
    """二元/一元运算的操作数：数字、日期或天数（date - date、date + days(n) 等）。"""
    if isinstance(x, (date, timedelta)):
        return x
    return _number(x)


def _format(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, timedelta):
        days = value.days + value.seconds / 86400
        return f"{days:g} days"
    if isinstance(value, date):
        return f"{value.isoformat()} ({_weekday(value)})"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return format(value, ".15g")
    return str(value)


# 千分位逗号：数字后紧跟逗号再紧跟正好三位数字（1,234,567）。只在括号外认，括号里的逗号是函数参数
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# 百分号：紧跟在数字后，且后面不是数字、名字或左括号（那样是取余：10 % 3）
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%(?!\s*[\w(.])")
MAX_EXPRESSIONS = 20


def _outside_parens(expr: str, pattern: re.Pattern, repl: str) -> str:
    """只替换括号外的部分。"""
    out, buf, depth = [], [], 0
    for ch in expr:
        if ch == "(" and depth == 0:
            out.append(pattern.sub(repl, "".join(buf)))
            buf = []
        buf.append(ch)
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0:
                out.append("".join(buf))
                buf = []
    tail = "".join(buf)
    out.append(pattern.sub(repl, tail) if depth == 0 else tail)
    return "".join(out)


def _normalize(expr: str) -> str:
    # 模型偶尔用数学书写习惯：^ 表示乘方、×÷ 表示乘除、末尾写个等号
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/").rstrip().rstrip("=")
    expr = _PERCENT_RE.sub(r"(\1/100)", expr)
    return _outside_parens(expr, _THOUSANDS_RE, "")


def _parts(expr: str) -> list[tuple[str, str | None, ast.expr]]:
    """一次给的几个算式（逗号、分号或换行隔开）拆成 (原文, 赋值给的变量名, 语法树)。
    「y = 算式」把结果存进变量 y，后面的算式可以用。"""
    try:
        module = ast.parse(expr, mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise CalcError("invalid syntax") from None
    parts = []
    for stmt in module.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            parts.append((stmt.targets[0].id, stmt.targets[0].id, stmt.value))
        elif isinstance(stmt, ast.Expr):
            nodes = stmt.value.elts if isinstance(stmt.value, ast.Tuple) else [stmt.value]
            parts += [(ast.get_source_segment(expr, n) or ast.unparse(n), None, n) for n in nodes]
        else:
            raise CalcError(f"unsupported syntax: {type(stmt).__name__}")
    if not parts:
        raise CalcError("empty expression")
    if len(parts) > MAX_EXPRESSIONS:
        raise CalcError("too many expressions")
    return parts


def _compute(node, env: dict):
    try:
        return _eval(node, env)
    except CalcError:
        raise
    except ZeroDivisionError:
        raise CalcError("division by zero") from None
    except RecursionError:
        raise CalcError("expression nested too deeply") from None
    except (OverflowError, ValueError, TypeError) as e:
        raise CalcError(str(e) or type(e).__name__) from None


def evaluate(expression: str) -> str:
    """计算表达式，返回结果文本；表达式非法时抛 CalcError。
    一次给了几个算式（逗号、分号或换行隔开）时逐个算，每行「算式 = 结果」，算不了的那行写原因；
    「名字 = 算式」把结果存进变量，后面的算式可以引用。"""
    expr = (expression or "").strip()
    if not expr:
        raise CalcError("empty expression")
    if len(expr) > MAX_EXPRESSION_CHARS:
        raise CalcError("expression too long")
    parts = _parts(_normalize(expr))
    env: dict = {}
    if len(parts) == 1:
        return _format(_compute(parts[0][2], env))
    lines, ok = [], False
    for text, var, node in parts:
        try:
            value = _compute(node, env)
        except CalcError as e:
            lines.append(f"{text}: error: {e}")
            continue
        if var is not None:
            env[var] = value
        lines.append(f"{text} = {_format(value)}")
        ok = True
    if not ok:
        raise CalcError("; ".join(lines))
    return "\n".join(lines)
