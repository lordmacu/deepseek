#!/usr/bin/env python3
"""
PoW solver — reads the challenge JSON from stdin, writes {"nonce": N, "prefix": P} to stdout.
Usage: echo '{"challenge":...,"salt":...,"difficulty":...,"expire_at":...}' | python3 pow_solver.py
"""
import sys, json, os, struct
from wasmer import engine, Store, Module, Instance
from wasmer_compiler_cranelift import Compiler

WASM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sha3_wasm_bg.wasm")

_instance = None

def _get_instance():
    global _instance
    if _instance is None:
        store = Store(engine.JIT(Compiler))
        with open(WASM_PATH, "rb") as f:
            wasm_bytes = f.read()
        module = Module(store, wasm_bytes)
        _instance = Instance(module)
    return _instance

def _mem(inst):
    return memoryview(inst.exports.memory.buffer)

def _write_str(inst, s):
    buf = s.encode("utf-8")
    n = len(buf)
    ptr = inst.exports.__wbindgen_export_0(n + 1, 1)
    m = _mem(inst)
    m[ptr:ptr + n] = buf
    m[ptr + n] = 0
    return ptr, n

def solve(challenge, salt, expire_at, difficulty):
    inst = _get_instance()
    prefix = f"{salt}_{expire_at}_"
    ret_ptr = inst.exports.__wbindgen_add_to_stack_pointer(-16)
    ptr0, len0 = _write_str(inst, challenge)
    ptr1, len1 = _write_str(inst, prefix)
    inst.exports.wasm_solve(ret_ptr, ptr0, len0, ptr1, len1, float(difficulty))
    m = _mem(inst)
    status = struct.unpack_from("<i", m, ret_ptr)[0]
    nonce  = struct.unpack_from("<d", m, ret_ptr + 8)[0]
    inst.exports.__wbindgen_add_to_stack_pointer(16)
    if status == 0:
        raise SystemExit("PoW solver: no solution found")
    return int(nonce), prefix

if __name__ == "__main__":
    data = json.loads(sys.stdin.buffer.read())
    expire_at = data.get("expire_at") or data.get("expireAt")
    nonce, prefix = solve(data["challenge"], data["salt"], expire_at, data["difficulty"])
    sys.stdout.write(json.dumps({"nonce": nonce, "prefix": prefix}))
