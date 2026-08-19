#!/usr/bin/env node
// Properly call wasm_deepseek_hash_v1 to find the actual hash algorithm
import { readFileSync } from 'fs';

const wasmBytes = readFileSync('/Users/cristian/deep/sha3_wasm_bg.wasm');
const importObject = {
  __wbindgen_placeholder__: { __wbindgen_throw: (p,l) => { throw new Error('wasm'); } },
  wbg: { __wbindgen_throw: (p,l) => { throw new Error('wasm'); } }
};

const { instance } = await WebAssembly.instantiate(wasmBytes, importObject);
const exp = instance.exports;
const mem = exp.memory;
const enc = new TextEncoder();
const dec = new TextDecoder('utf-8');

function u8()  { return new Uint8Array(mem.buffer); }
function i32() { return new Int32Array(mem.buffer); }

// Malloc with alignment
function malloc(size) { return exp.__wbindgen_export_0(size, 1); }

function writeStr(s) {
  const buf = enc.encode(s);
  const ptr = malloc(buf.length + 1);
  u8().set(buf, ptr);
  u8()[ptr + buf.length] = 0;
  return { ptr, len: buf.length };
}

function deepseekHash(input) {
  // Allocate retptr on heap (8 bytes: i32 dataPtr + i32 dataLen)
  const retPtr = malloc(8);
  i32()[retPtr/4] = 0;
  i32()[retPtr/4+1] = 0;

  const { ptr: inPtr, len: inLen } = writeStr(input);

  exp.wasm_deepseek_hash_v1(retPtr, inPtr, inLen);

  const dataPtr = i32()[retPtr/4];
  const dataLen = i32()[retPtr/4+1];

  if (dataPtr === 0 || dataLen === 0) {
    return null;
  }

  const raw = u8().slice(dataPtr, dataPtr + dataLen);
  return dec.decode(raw);
}

// Test known values
const known = [
  { input: '', sha3: 'a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a',
               keccak: 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470' },
  { input: 'abc', sha3: '3a985da74fe225b2045c172d6bd390bd855f086e3e9d525b46bfe24511431532',
                  keccak: '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45' },
  { input: 'hello', sha3: '3338be694f50c5f338814986cdf0686453a888b84f424d792af4b9202398f392',
                    keccak: '1c8aff950685c2ed4bc3174f3472287b56d9517b9c948127319a09a7a36deac8' },
];

console.log('WASM hash test:');
for (const t of known) {
  const h = deepseekHash(t.input);
  console.log(`  input="${t.input}"`);
  console.log(`    wasm:   ${h}`);
  console.log(`    sha3:   ${t.sha3}  ${h === t.sha3 ? '✓ SHA3-256' : ''}`);
  console.log(`    keccak: ${t.keccak}  ${h === t.keccak ? '✓ Keccak-256' : ''}`);
  console.log();
}
