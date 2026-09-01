"""Python/Native Pure Chart Timeline 差分工具。

对同一份 Bestdori 谱面分别用 Native（maabangdream_realtime.pyd）与 Python
参考实现编译动作序列，逐字段比较 kind、lane、contact、target_x、due_s 与
hold 生命周期（note_index + flick_direction）。

用法：
    python scripts/diff_native_timeline.py --chart resource/charts/.../306/hard.json

Native 未构建/不可导入时给出明确错误并返回非零退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.realtime.chart_timeline import ChartTimeline
from scripts.pure_chart_reference import compile_actions as reference_compile


def load_native_module():
    """导入 Native 模块；失败时抛出带指引的错误。"""
    native_dir = ROOT / "agent" / "realtime" / "native"
    if str(native_dir) not in sys.path:
        sys.path.insert(0, str(native_dir))
    try:
        import maabangdream_realtime  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "Native 模块不可用，请先运行 .\\scripts\\build_native_realtime.ps1"
        ) from exc
    return maabangdream_realtime


def compare_actions(
    reference: list[dict[str, object]],
    native: list[dict[str, object]],
    *,
    time_tolerance_s: float = 1e-9,
) -> dict[str, object]:
    mismatches: list[dict[str, object]] = []
    for index, (expected, actual) in enumerate(zip(reference, native)):
        fields = (
            "kind",
            "lane",
            "contact",
            "target_x",
            "note_index",
            "flick_direction",
        )
        problems = [
            field for field in fields
            if expected.get(field) != actual.get(field)
        ]
        if abs(
            float(expected["due_s"]) - float(actual["due_s"])
        ) > time_tolerance_s:
            problems.append("due_s")
        if problems:
            mismatches.append({
                "index": index,
                "expected": expected,
                "actual": actual,
                "fields": problems,
            })
    if len(reference) != len(native):
        mismatches.append({
            "index": min(len(reference), len(native)),
            "expected_count": len(reference),
            "actual_count": len(native),
            "fields": ["action_count"],
        })
    return {
        "reference_actions": len(reference),
        "native_actions": len(native),
        "mismatches": len(mismatches),
        "details": mismatches[:10],
    }


def diff_chart(chart_path: Path) -> dict[str, object]:
    native_module = load_native_module()
    timeline = ChartTimeline.from_json(chart_path)
    reference = reference_compile(timeline)
    native_timeline = native_module.ChartTimeline.from_file(str(chart_path))
    native = native_timeline.compile_actions({})
    return compare_actions(reference, native)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, required=True)
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="把完整报告写入该文件",
    )
    args = parser.parse_args()
    report = diff_chart(args.chart)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
