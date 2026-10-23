"""Copy access_setup.py into cloudoptics-access.yaml's inline Lambda code.

Run after editing the setup step: ``python infra/cloudformation/setup/embed.py``.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "cloudoptics-access.yaml"
START = "        ZipFile: |\n"
END = "\n  CleanUp:\n"


def main() -> None:
    text = TEMPLATE.read_text()
    code = (HERE / "access_setup.py").read_text()
    indented = "".join(
        f"          {line}" if line.strip() else "\n" for line in code.splitlines(True)
    )
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    TEMPLATE.write_text(head + START + indented + END + tail)


if __name__ == "__main__":
    main()
