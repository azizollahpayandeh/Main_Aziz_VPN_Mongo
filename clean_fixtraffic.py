from pathlib import Path
from datetime import datetime

p = Path("main.py")
s = p.read_text(encoding="utf-8")

backup = p.with_suffix(".py.bak-clean-fixtraffic-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
backup.write_text(s, encoding="utf-8", newline="\n")

decorator = '@dp.message(Command("fixtraffic"))'

new_func = r'''@dp.message(Command("fixtraffic"))
async def on_fixtraffic_cmd(message: Message) -> None:
    """
    Admin tool:
        /fixtraffic SUB_ID_OR_XUI_EMAIL

    It resets the displayed usage of one subscription to 0 by saving the current
    panel counter as baseline. It also sets the subscription status to active.
    """
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.answer(
            "فرمت درست:\n"
            "<code>/fixtraffic SUB_ID_OR_XUI_EMAIL</code>\n\n"
            "مثال:\n"
            "<code>/fixtraffic AzizVPN-testGig-9705-1cfx</code>",
            parse_mode="HTML",
        )
        return

    key = parts[1].strip()

    if ObjectId.is_valid(key):
        query: Dict[str, Any] = {"_id": ObjectId(key)}
    else:
        query = {"xui_email": key}

    sub = await subs_col.find_one(query)

    if not sub and not ObjectId.is_valid(key):
        sub = await subs_col.find_one({
            "$or": [
                {"xui_email": key},
                {"display_name": key},
                {"name": key},
            ]
        })

    if not sub:
        await message.answer("❌ اشتراک پیدا نشد.")
        return

    try:
        raw_used = int(await xui.traffic_for_sub(sub) or 0)
    except Exception as exc:
        await message.answer(
            "❌ خطا در خواندن مصرف از پنل:\n"
            f"<code>{h(exc)}</code>",
            parse_mode="HTML",
        )
        return

    await subs_col.update_one(
        {"_id": sub["_id"]},
        {
            "$set": {
                "status": "active",
                "traffic_baseline_used_bytes": raw_used,
                "raw_used_bytes_cache": raw_used,
                "used_bytes_cache": 0,
                "traffic_updated_at": now_utc(),
                "updated_at": now_utc(),
            }
        },
    )

    await message.answer(
        "✅ مصرف این اشتراک ریست شد و وضعیت آن فعال شد.\n"
        "از این به بعد مصرف از همین لحظه حساب می‌شود.\n\n"
        f"اشتراک: <code>{h(sub.get('xui_email') or '-')}</code>\n"
        f"Baseline: <code>{fmt_bytes(raw_used)}</code>",
        parse_mode="HTML",
    )

'''

removed = 0

while True:
    start = s.find(decorator)
    if start == -1:
        break

    candidates = []
    for marker in [
        "\n@dp.message(",
        "\n@dp.callback_query(",
        "\n# =====================================================\n# Main",
        "\nasync def main(",
    ]:
        pos = s.find(marker, start + len(decorator))
        if pos != -1:
            candidates.append(pos)

    if not candidates:
        raise SystemExit("Could not find end of /fixtraffic block")

    end = min(candidates)
    s = s[:start] + s[end:]
    removed += 1

insert_markers = [
    "\n# =====================================================\n# Main",
    "\nasync def main(",
]

insert_pos = -1
for marker in insert_markers:
    insert_pos = s.find(marker)
    if insert_pos != -1:
        break

if insert_pos == -1:
    raise SystemExit("Could not find place to insert clean /fixtraffic command")

s = s[:insert_pos] + "\n\n" + new_func + s[insert_pos:]

p.write_text(s, encoding="utf-8", newline="\n")

print(f"Removed broken fixtraffic blocks: {removed}")
print("Inserted clean fixtraffic command.")
print("Backup:", backup)
