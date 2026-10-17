"""把 pytest 的 JUnit 结果里失败的测试写成 GitHub 的错误标注：运行页面上直接能看到原因，
公开仓库不登录也能通过接口读到。用法：python annotate.py junit.xml
"""

from pathlib import Path
import sys
import xml.etree.ElementTree as ElementTree


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: Path) -> None:
    if not path.is_file():
        print(f"::error title=没有测试结果::{path} 不存在，测试可能没有运行")
        return
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        for problem in [*case.findall("failure"), *case.findall("error")]:
            name = f"{case.get('classname')}.{case.get('name')}"
            detail = f"{problem.get('message') or ''}\n{problem.text or ''}".strip()
            print(f"::error title={escape(name)}::{escape(detail[-4000:])}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
