from pathlib import Path
from datetime import datetime

p = Path("main.py")
s = p.read_text(encoding="utf-8")

backup = p.with_suffix(".py.bak-restore-cleanup-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
backup.write_text(s, encoding="utf-8", newline="\n")

cleanup_block = r'''
# =====================================================
# Cleanup loop
# =====================================================
async def cleanup_loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await cleanup_expired()
        except Exception:
            logger.exception("cleanup loop error")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def cleanup_expired() -> None:
    now = now_utc()

    async for sub in subs_col.find({"status": "active"}):
        used = await sub_used_bytes(sub)
        expires_at = ensure_aware_dt(sub.get("expires_at"))

        total_bytes = int(sub.get("total_bytes", 0) or 0)
        expired_by_time = bool(expires_at and expires_at <= now)
        expired_by_volume = bool(total_bytes > 0 and used >= total_bytes)

        if sub.get("is_trial") and (expired_by_time or expired_by_volume):
            try:
                await xui.delete_client(sub["xui_email"])
            except Exception:
                logger.exception("failed to delete expired trial client from x-ui")

            await subs_col.update_one(
                {"_id": sub["_id"]},
                {"$set": {
                    "status": "deleted",
                    "ended_at": now,
                    "deleted_at": now,
                    "updated_at": now,
                }},
            )

        elif expired_by_time or expired_by_volume:
            await subs_col.update_one(
                {"_id": sub["_id"]},
                {"$set": {
                    "status": "expired",
                    "ended_at": now,
                    "updated_at": now,
                }},
            )

    delete_before = now - timedelta(days=10)

    async for sub in subs_col.find({"status": "expired", "is_trial": {"$ne": True}}):
        ended_at = ensure_aware_dt(sub.get("ended_at"))
        if not ended_at or ended_at > delete_before:
            continue

        try:
            await xui.delete_client(sub["xui_email"])
        except Exception:
            logger.exception("failed to delete old expired client from x-ui")

        await subs_col.update_one(
            {"_id": sub["_id"]},
            {"$set": {
                "status": "deleted",
                "deleted_at": now,
                "updated_at": now,
            }},
        )

'''

marker = "\n# =====================================================\n# Main"
pos = s.find(marker)

if pos == -1:
    pos = s.find("\nasync def main()")
    if pos == -1:
        raise SystemExit("ERROR: Could not find main section")

# اگر cleanup_loop قبلاً وجود ندارد، اضافه کن
if "async def cleanup_loop()" not in s:
    s = s[:pos] + "\n\n" + cleanup_block + s[pos:]
    print("OK restored cleanup_loop and cleanup_expired")
else:
    print("cleanup_loop already exists, no insert needed")

p.write_text(s, encoding="utf-8", newline="\n")
print("Backup:", backup)
