"""把 pytest 的 JUnit 结果写成 GitHub 的标注：每个失败的测试一条错误，再加一条汇总（通过、失败、跳过各多少，
跳过的是哪些、为什么）。运行页面上直接能看到，公开仓库不登录也能通过接口读到。用法：python annotate.py junit.xml
"""

from collections import Counter
from pathlib import Path
import sys
import xml.etree.ElementTree as ElementTree


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: Path) -> None:
    if not path.is_file():
        print(f"::error title=没有测试结果::{path} 不存在，测试可能没有运行")
        return
    counts: Counter[str] = Counter()
    skipped: list[str] = []
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        name = f"{case.get('classname')}.{case.get('name')}"
        problems = [*case.findall("failure"), *case.findall("error")]
        for problem in problems:
            detail = f"{problem.get('message') or ''}\n{problem.text or ''}".strip()
            print(f"::error title={escape(name)}::{escape(detail[-4000:])}")
        skip = case.find("skipped")
        if problems:
            counts["失败"] += 1
        elif skip is not None:
            counts["跳过"] += 1
            skipped.append(f"{name}：{skip.get('message') or ''}")
        else:
            counts["通过"] += 1
    summary = "，".join(f"{kind} {counts[kind]}" for kind in ("通过", "失败", "跳过"))
    lines = [summary, *skipped]
    print(f"::notice title=测试结果::{escape(chr(10).join(lines))}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
