#!/usr/bin/env python3
"""
DeepSeek login test — reverse-engineered from APK v2.3.6
"""

import os
import json
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

EMAIL    = os.getenv("EMAIL", "yo@cristiangarcia.co")
PASSWORD = os.getenv("PASSWORD")

BASE_URL   = "https://chat.deepseek.com"
LOGIN_PATH = "/api/v0/users/login"

HEADERS = {
    "Content-Type":        "application/json",
    "User-Agent":          "DeepSeek/2.3.6 (com.deepseek.chat; Android)",
    "x-client-platform":   "android",
    "x-client-version":    "2.3.6",
    "x-client-bundle-id":  "com.deepseek.chat",
    "x-client-locale":     "en-US",
}

payload = {
    "email":     EMAIL,
    "password":  PASSWORD,
    "device_id": str(uuid.uuid4()),
    "os":        "android",
}


def main():
    print(f"[*] POST {BASE_URL}{LOGIN_PATH}")
    print(f"[*] email: {EMAIL}")

    resp = requests.post(
        BASE_URL + LOGIN_PATH,
        headers=HEADERS,
        json=payload,
        timeout=15,
    )

    print(f"[*] Status: {resp.status_code}")

    try:
        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))

        token = (
            data.get("data", {}).get("user_info", {}).get("token")
            or data.get("data", {}).get("token")
            or data.get("token")
        )
        if token:
            print(f"\n[+] Token: {token[:40]}...")
    except Exception:
        print(resp.text[:500])


if __name__ == "__main__":
    main()
