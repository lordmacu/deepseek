#!/usr/bin/env node
// Correct wasm_deepseek_hash_v1 calling convention:
// param 0 = retptr (8 bytes: i32 ptr + i32 len)
// param 1 = input ptr, param 2 = input len
// Returns hex string via (ptr, len) written at retptr

import { readFileSync } from 'fs';

const wasmBytes = readFileSync('/Users/cristian/deep/sha3_wasm_bg.wasm');
const importObject = {
  __wbindgen_placeholder__: { __wbindgen_throw: (p,l) => { throw new Error('wasm err'); } },
  wbg: { __wbindgen_throw: (p,l) => { throw new Error('wasm err'); } }
};

const wasmModule = await WebAssembly.instantiate(wasmBytes, importObject);
const exp = wasmModule.instance.exports;
const mem = exp.memory;

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8');

function getU8()  { return new Uint8Array(mem.buffer); }
function getI32() { return new Int32Array(mem.buffer); }

let WASM_VECTOR_LEN = 0;

function passStr(s) {
  const buf = enc.encode(s);
  const ptr = exp.__wbindgen_export_0(buf.length + 1, 1); // malloc(size, align)
  getU8().subarray(ptr, ptr + buf.length).set(buf);
  getU8()[ptr + buf.length] = 0;
  WASM_VECTOR_LEN = buf.length;
  return ptr;
}

function deepseekHash(input) {
  // retptr needs 8 bytes (4 for ptr, 4 for len)
  const retPtr = exp.__wbindgen_add_to_stack_pointer(-8);
  try {
    const inPtr = passStr(input);
    const inLen = WASM_VECTOR_LEN;
    exp.wasm_deepseek_hash_v1(retPtr, inPtr, inLen);

    // Read (data_ptr, data_len) from retptr
    const i32 = getI32();
    const dataPtr = i32[retPtr / 4];
    const dataLen = i32[retPtr / 4 + 1];

    // Read the hex string
    const hashHex = dec.decode(getU8().subarray(dataPtr, dataPtr + dataLen));

    // Free the allocated string buffer
    exp.__wbindgen_export_2(dataPtr, dataLen, 1); // free(ptr, size, align)
    exp.__wbindgen_export_2(inPtr, inLen + 1, 1);

    return hashHex;
  } finally {
    exp.__wbindgen_add_to_stack_pointer(8);
  }
}

// Test with known inputs to verify it's SHA3-256
const tests = [
  { input: '', expectedSha3: 'a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a' },
  { input: 'abc', expectedSha3: '3a985da74fe225b2045c172d6bd390bd855f086e3e9d525b46bfe24511431532' },
];

console.log('Testing wasm_deepseek_hash_v1:');
for (const t of tests) {
  const h = deepseekHash(t.input);
  const ok = h === t.expectedSha3 ? '✓' : '✗';
  console.log(`  ${ok} "${t.input}" → ${h}`);
  if (t.expectedSha3) console.log(`    expected: ${t.expectedSha3}`);
}

// Now hash the actual PoW input and check condition
const salt = 'c92d818d0be8c82a42a1';
const expireAt = 1786974074928;
const nonce = 52507;
const difficulty = 144000;
const challenge = '41e63c2e79a1429f7fc4c11e5c8142fe37e0d4807a6a8d62b78dfe81777a30d9';

const prefix = `${salt}_${expireAt}_`;
const inp = `${prefix}${nonce}`;
const h = deepseekHash(inp);

console.log(`\nPoW verification:`);
console.log(`  Input:     ${inp}`);
console.log(`  Hash:      ${h}`);
const hInt = BigInt('0x' + h);
console.log(`  hash % ${difficulty} = ${hInt % BigInt(difficulty)}`);
console.log(`  == 0? ${hInt % BigInt(difficulty) === 0n}`);
console.log(`  hash <= challenge? ${hInt <= BigInt('0x' + challenge)}`);

// Brute force a few to check the correct condition
console.log('\nSearching for real nonce (first match):');
let firstModMatch = -1;
for (let n = 0; n < 300000; n++) {
  const hh = deepseekHash(`${prefix}${n}`);
  const hi = BigInt('0x' + hh);
  if (hi % BigInt(difficulty) === 0n) {
    if (firstModMatch < 0) {
      firstModMatch = n;
      console.log(`  mod=0 match at nonce=${n}`);
      if (n === nonce) console.log('  *** MATCHES WASM ANSWER! ***');
    }
  }
  if (n === nonce) {
    const hi2 = BigInt('0x' + hh);
    console.log(`  At wasm nonce ${nonce}: hash % difficulty = ${hi2 % BigInt(difficulty)}`);
  }
  if (firstModMatch >= 0 && n > nonce + 10) break;
}
