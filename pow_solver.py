#!/usr/bin/env python3
"""
PoW solver — reads the challenge JSON from stdin, writes {"nonce": N, "prefix": P} to stdout.
Usage: echo '{"challenge":...,"salt":...,"difficulty":...,"expire_at":...}' | python3 pow_solver.py

Runtime: wasmtime. El paquete `wasmer` de PyPI esta abandonado y publica un stub que
lanza ImportError("Wasmer is not available on this system") cuando no hay wheel para
la plataforma/version de Python — pasa con Python 3.11, que es el del Dockerfile, asi
que el solver fallaba DENTRO del contenedor y con el toda peticion de chat.
"""
import sys, json, os, struct
from wasmtime import Engine, Store, Module, Instance

WASM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sha3_wasm_bg.wasm")

_store = None
_exports = None


def _get():
    global _store, _exports
    if _exports is None:
        engine = Engine()
        _store = Store(engine)
        module = Module.from_file(engine, WASM_PATH)
        instance = Instance(_store, module, [])
        _exports = instance.exports(_store)
    return _store, _exports


def _write_str(store, exports, s):
    buf = s.encode("utf-8")
    n = len(buf)
    ptr = exports["__wbindgen_export_0"](store, n + 1, 1)
    mem = exports["memory"]
    mem.write(store, buf, ptr)
    mem.write(store, b"\x00", ptr + n)
    return ptr, n


def solve(challenge, salt, expire_at, difficulty):
    store, exports = _get()
    prefix = f"{salt}_{expire_at}_"
    ret_ptr = exports["__wbindgen_add_to_stack_pointer"](store, -16)
    ptr0, len0 = _write_str(store, exports, challenge)
    ptr1, len1 = _write_str(store, exports, prefix)
    exports["wasm_solve"](store, ret_ptr, ptr0, len0, ptr1, len1, float(difficulty))
    raw = exports["memory"].read(store, ret_ptr, ret_ptr + 16)
    status = struct.unpack_from("<i", raw, 0)[0]
    nonce = struct.unpack_from("<d", raw, 8)[0]
    exports["__wbindgen_add_to_stack_pointer"](store, 16)
    if status == 0:
        raise SystemExit("PoW solver: no solution found")
    return int(nonce), prefix


if __name__ == "__main__":
    data = json.loads(sys.stdin.buffer.read())
    expire_at = data.get("expire_at") or data.get("expireAt")
    nonce, prefix = solve(data["challenge"], data["salt"], expire_at, data["difficulty"])
    sys.stdout.write(json.dumps({"nonce": nonce, "prefix": prefix}))
