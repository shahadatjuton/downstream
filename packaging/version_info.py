"""Generate the Windows VERSIONINFO resource file PyInstaller embeds into the exe
(what you see under Properties → Details). Usage:

    python packaging/version_info.py build/version_info.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_version() -> str:
    text = (ROOT / "src" / "idm" / "__init__.py").read_text("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("could not find __version__ in src/idm/__init__.py")
    return m.group(1)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", version)[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt},
    prodvers={vt},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'IDM Clone open-source project'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{internal}'),
         StringStruct('LegalCopyright', 'MIT License. Not affiliated with Tonec Inc.'),
         StringStruct('OriginalFilename', '{filename}'),
         StringStruct('ProductName', 'IDM Clone'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "version_info.txt"
    which = sys.argv[2] if len(sys.argv) > 2 else "app"
    version = read_version()
    if which == "host":
        desc, internal, filename = "IDM Clone browser native messaging host", "idm-host", "idm-host.exe"
    else:
        desc, internal, filename = "IDM Clone download manager", "IDM Clone", "IDM Clone.exe"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        TEMPLATE.format(
            vt=version_tuple(version), version=version, description=desc, internal=internal, filename=filename
        ),
        "utf-8",
    )
    print(f"{out} ({version})")


if __name__ == "__main__":
    main()
