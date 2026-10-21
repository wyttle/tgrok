"""calculate：精确计算算式和日期，避免模型心算出错。"""

from .. import calc
from ..i18n import t
from . import Outcome, Tool, failed


class CalculateTool(Tool):
    name = "calculate"
    description = (
        "Evaluate a math or date expression exactly. Use it instead of mental math whenever "
        "an answer depends on arithmetic, percentages, unit conversions, or date math, "
        "including checking numbers someone else claimed. Supports + - * / // % ** and "
        "parentheses, comparisons (< <= > >= == !=) that return true/false, abs round min max "
        "sqrt cbrt exp log(x, base) log10 log2 sin cos tan asin acos atan radians degrees "
        "floor ceil factorial gcd lcm, constants pi and e, and dates: date('YYYY-MM-DD'), "
        "today(), days(n), weekday(date). Subtracting two dates gives days; date +/- days(n) "
        "gives a date. Percentages like 15% and thousands separators like 1,234 are understood. "
        "Several expressions can go in one call, separated by ';' or new lines; each gets its own result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "One or more expressions, e.g. 1299*0.85 or date('2026-12-25') - today(); "
                               "separate several with ';'.",
            }
        },
        "required": ["expression"],
    }

    def prompt_note(self) -> str:
        return t("calc_system_prompt")

    def label(self, args: dict) -> str:
        return t("tool_calc", expr=(str(args.get("expression", "")).strip() or "?")[:48])

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        expression = str(args.get("expression", ""))
        try:
            value = calc.evaluate(expression)
        except calc.CalcError as e:
            return failed(t("calc_error", error=str(e)))
        if "\n" in value:  # 一次算了几个：每行已经是「算式 = 结果」
            return Outcome(value, True, t("res_results", n=value.count("\n") + 1))
        return Outcome(f"{expression.strip()} = {value}", True, "= " + value[:40])
