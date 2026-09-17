"""Keep in static/lib/iconoir.css only the icons the templates and scripts use.

The full Iconoir sheet weighs 5.7 MB for 3000+ icons, and the site shows
fifty: shipped whole, it lands after the first paint and the icons blink in
on a hard reload. Run after adding an icon, with the full sheet as argument:

    uv run python scripts/subset_iconoir.py /path/to/iconoir/css/iconoir.css
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static/lib/iconoir.css"
SOURCES = [ROOT / "website/templates", ROOT / "static/js", ROOT / "src"]


def used_icons():
    names = set()
    for source in SOURCES:
        for path in source.rglob("*"):
            if path.is_file():
                names.update(re.findall(r"iconoir-([a-z0-9-]+)", path.read_text(errors="ignore")))
    return names


def main(full_sheet):
    css = Path(full_sheet).read_text()
    icons = used_icons()
    icon_rule = re.compile(r"\.iconoir-([a-z0-9-]+)::before\{[^}]*\}\n?")
    found = {m.group(1) for m in icon_rule.finditer(css)}
    if missing := icons - found:
        sys.exit(f"unknown icons: {', '.join(sorted(missing))}")
    # Everything that is not an icon rule (the header, the base rules) stays.
    OUT.write_text(icon_rule.sub(lambda m: m.group(0) if m.group(1) in icons else "", css))
    print(f"{len(icons)} icons kept, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1])
