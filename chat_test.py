#!/usr/bin/env python3
"""
DeepSeek chat — PoW resuelto con WASM nativo via Node.js subprocess.
Reverse-engineered de APK v2.3.6 + web worker sha3_wasm_bg.wasm

Algoritmo PoW (DeepSeekHashV1):
  prefix = salt + "_" + expire_at + "_"
  Encuentra nonce tal que wasm_solve(challenge, prefix, difficulty) converge.
  El WASM usa SHA3 internamente; no replicado aquí — se llama al WASM directo.
"""

import os, json, uuid, subprocess, base64, requests
from dotenv import load_dotenv

load_dotenv("/Users/cristian/deep/.env")

BASE = "https://chat.deepseek.com"
HEADERS_BASE = {
    "Content-Type":       "application/json",
    "User-Agent":         "DeepSeek/2.3.6 (com.deepseek.chat; Android)",
    "x-client-platform":  "android",
    "x-client-version":   "2.3.6",
    "x-client-bundle-id": "com.deepseek.chat",
}
POW_SOLVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pow_solver.mjs")


def login():
    resp = requests.post(BASE + "/api/v0/users/login", headers=HEADERS_BASE, json={
        "email":     os.getenv("EMAIL", "yo@cristiangarcia.co"),
        "password":  os.getenv("PASSWORD"),
        "device_id": str(uuid.uuid4()),
        "os":        "android",
    }, timeout=10)
    data = resp.json()["data"]["biz_data"]["user"]
    print(f"[+] Login OK — {data['id_profile']['name']}")
    return data["token"]


def get_pow_challenge(headers):
    resp = requests.post(BASE + "/api/v0/chat/create_pow_challenge", headers=headers,
        json={"target_path": "/api/v0/chat/completion"}, timeout=10)
    return resp.json()["data"]["biz_data"]["challenge"]


def solve_pow(challenge: dict) -> int:
    """Usa el WASM de DeepSeek via Node.js subprocess para resolver el PoW."""
    result = subprocess.run(
        ["node", POW_SOLVER],
        input=json.dumps(challenge).encode(),
        capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pow_solver failed: {result.stderr.decode()}")
    return json.loads(result.stdout)["nonce"]


def build_pow_header(challenge: dict, nonce: int) -> str:
    """Construye X-DS-PoW-Response: base64(JSON con el nonce resuelto)."""
    payload = {
        "algorithm":   challenge["algorithm"],
        "challenge":   challenge["challenge"],
        "salt":        challenge["salt"],
        "signature":   challenge["signature"],
        "answer":      nonce,
        "target_path": challenge["target_path"],
    }
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(raw.encode()).decode()


def create_session(headers):
    resp = requests.post(BASE + "/api/v0/chat_session/create", headers=headers, json={}, timeout=10)
    session_id = resp.json()["data"]["biz_data"]["chat_session"]["id"]
    print(f"[+] Sesión: {session_id}")
    return session_id


def chat(headers, session_id, prompt):
    """Envía un mensaje y muestra la respuesta en streaming."""
    print(f"\n[*] Obteniendo PoW challenge...")
    ch = get_pow_challenge(headers)
    print(f"    difficulty={ch['difficulty']}")

    print(f"[*] Resolviendo PoW con WASM...")
    nonce = solve_pow(ch)
    print(f"[+] nonce={nonce}")

    req_headers = {**headers, "X-DS-PoW-Response": build_pow_header(ch, nonce)}

    payload = {
        "chat_session_id":   session_id,
        "parent_message_id": None,
        "prompt":            prompt,
        "ref_file_ids":      [],
        "search_enabled":    False,
        "thinking_enabled":  False,
        "stream":            True,
    }

    print(f"\n[*] Enviando: {prompt!r}")
    print("[*] Respuesta: ", end="", flush=True)

    resp = requests.post(
        BASE + "/api/v0/chat/completion",
        headers=req_headers,
        json=payload,
        stream=True,
        timeout=120,
    )

    if resp.status_code != 200:
        print(f"\n[!] Error HTTP {resp.status_code}: {resp.text[:300]}")
        return

    # SSE patch format:
    #   data: {"p":"response/fragments/-1/content","o":"APPEND","v":"text..."}
    #   data: {"v":"more text"}   ← continuación sin path/op
    in_content = False
    for line in resp.iter_lines():
        raw = line.decode() if isinstance(line, bytes) else line
        if not raw.startswith("data:"):
            continue
        try:
            evt = json.loads(raw[5:])
        except Exception:
            continue

        # Detectar inicio de contenido
        path = evt.get("p", "")
        if "content" in path and evt.get("o") == "APPEND":
            in_content = True

        # Fin del stream
        if evt.get("p") == "response/status" and evt.get("v") == "FINISHED":
            in_content = False
            break

        # Imprimir fragmentos de texto
        if in_content and "v" in evt and isinstance(evt["v"], str) and "p" not in evt:
            print(evt["v"], end="", flush=True)
        elif in_content and "v" in evt and path.endswith("content"):
            print(evt["v"], end="", flush=True)

    print()  # newline al final


def main():
    token = login()
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {token}"}
    session_id = create_session(headers)
    chat(headers, session_id, "Explica brevemente qué es la inteligencia artificial.")


if __name__ == "__main__":
    main()
