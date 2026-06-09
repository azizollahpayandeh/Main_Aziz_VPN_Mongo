from pathlib import Path
import re

p = Path("main.py")
s = p.read_text(encoding="utf-8")

s2 = re.sub(
    r'"فرمت درست:\s*\r?\n\s*<code>(/fixtraffic[^<]*)</code>"',
    r'"فرمت درست:\\n<code>\1</code>"',
    s
)

if s2 == s:
    print("No replacement made. Open main.py around line 2709 manually.")
else:
    p.write_text(s2, encoding="utf-8", newline="\n")
    print("Fixed broken /fixtraffic string.")
