#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AzizVPN 3X-UI traffic display fixer.

Usage on your server:
    python3 azizvpn_traffic_fix_patch.py bot.py

It creates a backup next to your bot file and then patches the traffic logic.
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


NEW_USAGE_BLOCK = r'''def normalize_used_with_baseline(sub: Dict[str, Any], raw_used: int) -> Tuple[int, int, Dict[str, Any]]:
    """
    Convert raw 3X-UI traffic to the usage shown to the user.

    Why this exists:
    some 3X-UI builds may return stale counters or an old counter after a
    client is recreated.  For every new subscription we save the panel counter
    at creation time as a baseline. Displayed usage is:
        current_raw_counter - creation_baseline
    """
    raw = max(int(raw_used or 0), 0)
    baseline = max(int(sub.get("traffic_baseline_used_bytes", 0) or 0), 0)
    patch: Dict[str, Any] = {}

    # If the panel counter was reset manually, never show negative usage.
    if baseline and raw < baseline:
        baseline = raw
        patch["traffic_baseline_used_bytes"] = baseline

    used = max(raw - baseline, 0)
    return used, raw, patch


async def sub_used_bytes(sub: Dict[str, Any]) -> int:
    """
    Read usage from 3X-UI and cache it in MongoDB.

    Important fix:
    - When 3X-UI is reachable, trust the current resolved client traffic.
      Do NOT keep an old higher cache forever, because a previous bug could
      cache the whole inbound traffic and show impossible values like
      68 GB used on a fresh 15 GB plan.
    - When 3X-UI is unavailable, show the last cached displayed usage.
    """
    raw_used_opt = await xui.traffic_for_sub(sub)
    cached = int(sub.get("used_bytes_cache", 0) or 0)

    if raw_used_opt is None:
        return cached

    used, raw_used, extra_patch = normalize_used_with_baseline(sub, int(raw_used_opt or 0))

    if sub.get("_id"):
        patch = {
            "used_bytes_cache": used,
            "raw_used_bytes_cache": raw_used,
            "traffic_updated_at": now_utc(),
            "updated_at": now_utc(),
            **extra_patch,
        }
        await subs_col.update_one({"_id": sub["_id"]}, {"$set": patch})
        sub.update(patch)

    return used


async def sync_subscription_from_xui(sub: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort sync of Mongo subscription data from the panel.

    This keeps total/expiry/uuid aligned, but usage is normalized by baseline
    so a stale panel/inbound counter cannot be shown as a user's real usage.
    """
    email = str(sub.get("xui_email") or "").strip()
    if not email or not sub.get("_id"):
        return sub

    try:
        snap = await xui.client_snapshot(email)
    except Exception as exc:
        logger.warning("subscription sync failed for %s: %s", email, exc)
        return sub

    if not snap:
        return sub

    patch: Dict[str, Any] = {"updated_at": now_utc()}

    total_bytes = int(snap.get("total_bytes") or 0)
    if total_bytes > 0:
        patch["total_bytes"] = total_bytes
        patch["volume_gb"] = int(round(total_bytes / (1024 ** 3)))

    expiry_ms = int(snap.get("expiry_ms") or 0)
    if expiry_ms > 0:
        patch["expires_at"] = ms_to_dt(expiry_ms)

    if snap.get("uuid"):
        patch["uuid"] = snap["uuid"]
    if snap.get("sub_id"):
        patch["sub_id"] = snap["sub_id"]

    if snap.get("traffic_found"):
        used, raw_used, extra_patch = normalize_used_with_baseline(sub, int(snap.get("used_bytes") or 0))
        patch["used_bytes_cache"] = used
        patch["raw_used_bytes_cache"] = raw_used
        patch["traffic_updated_at"] = now_utc()
        patch.update(extra_patch)

    if len(patch) > 1:
        await subs_col.update_one({"_id": sub["_id"]}, {"$set": patch})
        sub.update(patch)

    return sub


'''


NEW_TREE_METHOD = r'''    @staticmethod
    def _traffic_candidates_from_tree(node: Any, email: str, depth: int = 0) -> List[Dict[str, Any]]:
        """
        Recursively find traffic objects that match the target email.

        Critical fix:
        Never accept a traffic object without an email while scanning big trees
        such as /inbounds/list.  Inbound objects can also have up/down counters,
        and accepting those email-less counters makes the bot show the whole
        inbound traffic as one user's usage.
        """
        if depth > 10:
            return []

        found: List[Dict[str, Any]] = []

        if isinstance(node, dict):
            parsed = XUIClient._parse_traffic_obj(node)
            node_email = (
                (node.get("traffic") or {}).get("email")
                if isinstance(node.get("traffic"), dict)
                else node.get("email")
            )
            if parsed is not None:
                parsed_email = parsed.get("email") or node_email
                if parsed_email and XUIClient._email_matches(parsed_email, email):
                    found.append(parsed)

            for value in node.values():
                found.extend(XUIClient._traffic_candidates_from_tree(value, email, depth + 1))

        elif isinstance(node, list):
            for item in node:
                found.extend(XUIClient._traffic_candidates_from_tree(item, email, depth + 1))

        return found

'''


NEW_TRAFFIC_METHOD = r'''    async def traffic(self, email: str, inbound_id: int = INBOUND_ID) -> Optional[Dict[str, Any]]:
        if not email:
            return None

        email = str(email).strip()
        encoded = quote(email, safe="")
        candidates: List[Dict[str, Any]] = []

        # Direct client-traffic endpoints. We collect candidates, not return immediately.
        attempts: List[Tuple[str, str]] = [
            ("inbounds.getClientTraffics", f"/panel/api/inbounds/getClientTraffics/{encoded}"),
            ("clients.traffic", f"/panel/api/clients/traffic/{encoded}"),
            ("clients.get", f"/panel/api/clients/get/{encoded}"),
        ]
        for label, path in attempts:
            try:
                data = await self.request("GET", path)
                obj = data.get("obj", data)

                # Direct endpoints may return one client traffic object without
                # repeating email. Accept email-less traffic ONLY here.
                direct = self._parse_traffic_obj(obj)
                if direct is not None:
                    direct_email = direct.get("email") or (obj.get("email") if isinstance(obj, dict) else None)
                    if not direct_email or self._email_matches(direct_email, email):
                        candidates.append(direct)

                # Recursive scanning must only accept objects with matching email.
                found = self._traffic_candidates_from_tree(obj, email)
                if found:
                    candidates.extend(found)

                if direct is not None or found:
                    best = self._best_traffic([direct, *found])
                    if best:
                        logger.info(
                            "traffic candidate via %s for %s: used=%s up=%s down=%s",
                            label, email, self.traffic_used_bytes(best), best.get("up"), best.get("down"),
                        )
            except Exception as exc:
                logger.warning("traffic fetch failed via %s for %s: %s", label, email, exc)

        # Paged/list clients endpoints. Only matching-email objects are accepted.
        for label, path in (
            ("clients.list.paged", f"/panel/api/clients/list/paged?search={encoded}&pageSize=50&page=1"),
            ("clients.list", "/panel/api/clients/list"),
        ):
            try:
                data = await self.request("GET", path)
                items = self._extract_items_from_response(data)
                for item in items:
                    item_email = item.get("email") or ((item.get("client") or {}).get("email") if isinstance(item.get("client"), dict) else None)
                    if self._email_matches(item_email, email):
                        parsed = self._parse_traffic_obj(item)
                        if parsed is not None:
                            candidates.append(parsed)
                        candidates.extend(self._traffic_candidates_from_tree(item, email))
            except Exception as exc:
                logger.warning("traffic fetch failed via %s for %s: %s", label, email, exc)

        # Inbound clientStats are usually reliable, but do NOT accept the inbound's
        # own up/down counters because they belong to the whole inbound, not user.
        try:
            inbounds = [await self.get_inbound(inbound_id)]
            try:
                all_inbounds = await self.list_inbounds()
                seen_ids = {str(x.get("id")) for x in inbounds}
                inbounds.extend([x for x in all_inbounds if str(x.get("id")) not in seen_ids])
            except Exception:
                pass

            for inbound in inbounds:
                for stat in self._client_stats(inbound):
                    if self._email_matches(stat.get("email"), email):
                        parsed = self._parse_traffic_obj(stat)
                        if parsed is not None:
                            candidates.append(parsed)

                for client in self._settings_clients(inbound):
                    if self._email_matches(client.get("email"), email):
                        candidates.extend(self._traffic_candidates_from_tree(client, email))
        except Exception as exc:
            logger.warning("traffic fetch failed via inbounds for %s: %s", email, exc)

        best = self._best_traffic(candidates)
        if best is not None:
            logger.info(
                "traffic resolved for %s: used=%s up=%s down=%s candidates=%s",
                email,
                self.traffic_used_bytes(best),
                best.get("up"),
                best.get("down"),
                len(candidates),
            )
            return best

        logger.warning("traffic data not found for %s after all fallbacks", email)
        return None

'''


FIXTRAFFIC_COMMAND = r'''

@dp.message(Command("fixtraffic"))
async def on_fixtraffic_cmd(message: Message) -> None:
    """
    Admin tool:
        /fixtraffic SUB_ID_OR_XUI_EMAIL

    It resets the displayed usage of one subscription to 0 by saving the current
    panel counter as baseline. Use it for configs that were already created
    before this fix and have bad cached usage.
    """
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.answer(
            "فرمت درست:\n<code>/fixtraffic SUB_ID_OR_XUI_EMAIL</code>\n\n"
            "مثال:\n<code>/fixtraffic AzizVPN-testGig-9705-1cfx</code>",
            parse_mode="HTML",
        )
        return

    key = parts[1].strip()
    query: Dict[str, Any]
    if ObjectId.is_valid(key):
        query = {"_id": ObjectId(key)}
    else:
        query = {"xui_email": key}

    sub = await subs_col.find_one(query)
    if not sub:
        await message.answer("❌ اشتراک پیدا نشد.")
        return

    try:
        raw_used = int(await xui.traffic_for_sub(sub) or 0)
    except Exception as exc:
        await message.answer(f"❌ خطا در خواندن مصرف از پنل:\n<code>{h(exc)}</code>", parse_mode="HTML")
        return

    await subs_col.update_one(
        {"_id": sub["_id"]},
        {"$set": {
            "traffic_baseline_used_bytes": raw_used,
            "raw_used_bytes_cache": raw_used,
            "used_bytes_cache": 0,
            "traffic_updated_at": now_utc(),
            "updated_at": now_utc(),
        }},
    )

    await message.answer(
        "✅ مصرف این اشتراک ریست شد و از این به بعد از همین لحظه حساب می‌شود.\n\n"
        f"اشتراک: <code>{h(sub.get('xui_email'))}</code>\n"
        f"Baseline: <code>{fmt_bytes(raw_used)}</code>",
        parse_mode="HTML",
    )
'''


def replace_one(text: str, pattern: str, replacement: str, name: str, flags: int = re.S) -> tuple[str, bool]:
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        print(f"[WARN] Could not patch: {name}")
        return text, False
    print(f"[OK] Patched: {name}")
    return new_text, True


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 azizvpn_traffic_fix_patch.py /path/to/bot.py")
        return 2

    path = Path(sys.argv[1]).expanduser().resolve()
    if not path.exists():
        print(f"File not found: {path}")
        return 2

    text = path.read_text(encoding="utf-8")
    if "AzizVPN 3X-UI traffic baseline fix v2" in text:
        print("Already patched. Nothing to do.")
        return 0

    backup = path.with_suffix(path.suffix + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    print(f"Backup created: {backup}")

    ok_all = True

    # 1) Replace usage/cache/sync block.
    text, ok = replace_one(
        text,
        r'async def sub_used_bytes\(sub: Dict\[str, Any\]\) -> int:.*?\n\ndef join_channel_kb\(\) -> InlineKeyboardMarkup:',
        NEW_USAGE_BLOCK + 'def join_channel_kb() -> InlineKeyboardMarkup:',
        "sub_used_bytes + sync_subscription_from_xui",
    )
    ok_all &= ok

    # 2) Replace recursive candidate scanner.
    text, ok = replace_one(
        text,
        r'    @staticmethod\n    def _traffic_candidates_from_tree\(node: Any, email: str, depth: int = 0\) -> List\[Dict\[str, Any\]\]:.*?\n(?=    @staticmethod\n    def _best_traffic)',
        NEW_TREE_METHOD,
        "XUIClient._traffic_candidates_from_tree",
    )
    ok_all &= ok

    # 3) Replace full traffic method.
    text, ok = replace_one(
        text,
        r'    async def traffic\(self, email: str, inbound_id: int = INBOUND_ID\) -> Optional\[Dict\[str, Any\]\]:.*?\n(?=    async def traffic_for_sub)',
        NEW_TRAFFIC_METHOD,
        "XUIClient.traffic",
    )
    ok_all &= ok

    # 4) Purchase creation baseline.
    if "traffic_baseline_used_bytes" not in text:
        text, ok = replace_one(
            text,
            r'(if kind == "purchase":.*?result = await xui\.add_client\(.*?\n        \)\n)        sub = \{',
            r'''\1
        # Save current panel counter as creation baseline.
        # This protects fresh configs from old/stale 3X-UI traffic counters.
        try:
            baseline_used = int(await xui.traffic_for_sub({"xui_email": email}) or 0)
        except Exception as exc:
            logger.warning("could not read creation traffic baseline for %s: %s", email, exc)
            baseline_used = 0

        sub = {''',
            "purchase baseline read",
        )
        ok_all &= ok

        text, ok = replace_one(
            text,
            r'("order_id": order\["_id"\],\n)            "created_at": now_utc\(\),',
            r'''\1            "traffic_baseline_used_bytes": baseline_used,
            "raw_used_bytes_cache": baseline_used,
            "used_bytes_cache": 0,
            "traffic_updated_at": now_utc(),
            "created_at": now_utc(),''',
            "purchase baseline fields",
        )
        ok_all &= ok
    else:
        print("[INFO] Baseline fields already found, skipping purchase/trial field insertion.")

    # 5) Trial creation baseline. Only add if trial does not have baseline fields yet.
    if "could not read trial traffic baseline" not in text:
        text, ok = replace_one(
            text,
            r'(async def create_trial\(call: CallbackQuery\) -> None:.*?result = await xui\.add_client\(.*?\n        \)\n)        sub = \{',
            r'''\1
        try:
            baseline_used = int(await xui.traffic_for_sub({"xui_email": email}) or 0)
        except Exception as exc:
            logger.warning("could not read trial traffic baseline for %s: %s", email, exc)
            baseline_used = 0

        sub = {''',
            "trial baseline read",
        )
        ok_all &= ok

        text, ok = replace_one(
            text,
            r'("auto_renew": False,\n)            "created_at": now_utc\(\),',
            r'''\1            "traffic_baseline_used_bytes": baseline_used,
            "raw_used_bytes_cache": baseline_used,
            "used_bytes_cache": 0,
            "traffic_updated_at": now_utc(),
            "created_at": now_utc(),''',
            "trial baseline fields",
        )
        ok_all &= ok

    # 6) Add admin reset command.
    if '@dp.message(Command("fixtraffic"))' not in text:
        text, ok = replace_one(
            text,
            r'\n# =====================================================\n# Cleanup loop\n# =====================================================',
            FIXTRAFFIC_COMMAND + '\n\n# =====================================================\n# Cleanup loop\n# =====================================================',
            "admin /fixtraffic command",
        )
        ok_all &= ok
    else:
        print("[INFO] /fixtraffic already exists.")

    text = "# AzizVPN 3X-UI traffic baseline fix v2\n" + text
    path.write_text(text, encoding="utf-8")

    if ok_all:
        print("\nDone. Now restart your bot/service.")
        print("For the already broken subscription, send this to your bot as admin:")
        print("/fixtraffic AzizVPN-testGig-9705-1cfx")
        return 0

    print("\nPatch finished with warnings. Your backup is safe. Check the file before restart.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
