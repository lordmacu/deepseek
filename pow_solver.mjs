#!/usr/bin/env node
/**
 * PoW solver helper — lee JSON del challenge desde stdin, devuelve el nonce en stdout.
 * Uso: echo '{"challenge":...,"salt":...,"difficulty":...,"expireAt":...}' | node pow_solver.mjs
 */
import { readFileSync } from 'fs';

const wasmBytes = readFileSync(new URL('./sha3_wasm_bg.wasm', import.meta.url));
const importObject = {
  __wbindgen_placeholder__: { __wbindgen_throw: (p, l) => { throw new Error('wasm'); } },
  wbg: { __wbindgen_throw: (p, l) => { throw new Error('wasm'); } },
};

const { instance } = await WebAssembly.instantiate(wasmBytes, importObject);
const exp = instance.exports;
const mem = exp.memory;
const enc = new TextEncoder();

let WASM_LEN = 0;

function u8()  { return new Uint8Array(mem.buffer); }
function i32() { return new Int32Array(mem.buffer); }
function f64() { return new Float64Array(mem.buffer); }

function passStr(s) {
  const buf = enc.encode(s);
  const ptr = exp.__wbindgen_export_0(buf.length + 1, 1);
  u8().set(buf, ptr);
  u8()[ptr + buf.length] = 0;
  WASM_LEN = buf.length;
  return ptr;
}

function wasm_solve(challenge, prefix, difficulty) {
  const retPtr = exp.__wbindgen_add_to_stack_pointer(-16);
  try {
    const ptr0 = passStr(challenge);
    const len0 = WASM_LEN;
    const ptr1 = passStr(prefix);
    const len1 = WASM_LEN;
    exp.wasm_solve(retPtr, ptr0, len0, ptr1, len1, difficulty);
    const status = i32()[retPtr / 4];
    const nonce  = f64()[retPtr / 8 + 1];
    return status === 0 ? null : nonce;
  } finally {
    exp.__wbindgen_add_to_stack_pointer(16);
  }
}

// Read challenge JSON from stdin
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString());

const { challenge, salt, difficulty } = input;
// API returns expire_at (snake_case); web worker uses expireAt (camelCase)
const expireAt = input.expire_at ?? input.expireAt;

const prefix = `${salt}_${expireAt}_`;
const nonce = wasm_solve(challenge, prefix, difficulty);

if (nonce === null) {
  process.stderr.write('ERROR: no solution found\n');
  process.exit(1);
}

process.stdout.write(JSON.stringify({ nonce, prefix }));
