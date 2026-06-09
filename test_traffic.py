"""Quick diagnostic for XUI traffic API. Run: python test_traffic.py [email]"""
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from main import XUIClient, xui  # noqa: E402


async def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else "AzizVPN-TestHuge-0371-y8rf"
    sub = {"xui_email": email, "sub_id": sys.argv[2] if len(sys.argv) > 2 else None}
    print(f"Testing traffic for: {email}")
    traffic = await xui.traffic(email)
    print("traffic payload:", traffic)
    print("used bytes:", XUIClient.traffic_used_bytes(traffic))
    print("traffic_for_sub:", await xui.traffic_for_sub(sub))


if __name__ == "__main__":
    asyncio.run(main())
