#!/usr/bin/env node
// Use WASM to hash known inputs and figure out the algorithm
import { readFileSync } from 'fs';

const wasmBytes = readFileSync('/Users/cristian/deep/sha3_wasm_bg.wasm');
const importObject = { __wbindgen_placeholder__: { __wbindgen_throw: (p,l) => { throw new Error('wasm'); } }, wbg: { __wbindgen_throw: (p,l) => { throw new Error('wasm'); } } };

const wasmModule = await WebAssembly.instantiate(wasmBytes, importObject);
const exp = wasmModule.instance.exports;
const mem = exp.memory;

function getU8() { return new Uint8Array(mem.buffer); }

function encodeStr(s) {
  const buf = new TextEncoder().encode(s);
  const ptr = exp.__wbindgen_export_0(buf.length + 1, 1);
  getU8().subarray(ptr, ptr + buf.length).set(buf);
  return { ptr, len: buf.length };
}

// Call wasm_deepseek_hash_v1(out_ptr, in_ptr, in_len) → writes 32 bytes to out_ptr
function deepseekHash(input) {
  const { ptr: inPtr, len: inLen } = encodeStr(input);
  const outPtr = exp.__wbindgen_export_0(32, 1);
  exp.wasm_deepseek_hash_v1(outPtr, inPtr, inLen);
  const hashBytes = getU8().slice(outPtr, outPtr + 32);
  return Array.from(hashBytes).map(b => b.toString(16).padStart(2, '0')).join('');
}

// Test with known inputs
const testInputs = [
  '',
  'hello',
  'abc',
  'c92d818d0be8c82a42a1_1786974074928_52507',
  'c92d818d0be8c82a42a1_1786974074928_0',
  'c92d818d0be8c82a42a1_1786974074928_1',
];

console.log('wasm_deepseek_hash_v1 results:');
for (const inp of testInputs) {
  const h = deepseekHash(inp);
  console.log(`  SHA("${inp.substring(0,60)}")\n  = ${h}\n`);
}

// Now verify nonce 52507 with the correct difficulty condition
const challenge = '41e63c2e79a1429f7fc4c11e5c8142fe37e0d4807a6a8d62b78dfe81777a30d9';
const prefix = 'c92d818d0be8c82a42a1_1786974074928_';
const nonce = 52507;
const difficulty = 144000;

const inp = prefix + nonce;
const hash = deepseekHash(inp);
const hashBigInt = BigInt('0x' + hash);
const mod = Number(hashBigInt % BigInt(difficulty));

console.log(`\nVerify nonce ${nonce}:`);
console.log(`  Input: ${inp}`);
console.log(`  Hash:  ${hash}`);
console.log(`  hash % difficulty = ${mod}`);
console.log(`  == 0? ${mod === 0}`);

// Try: hash <= challenge
const challengeInt = BigInt('0x' + challenge);
console.log(`  hash <= challenge? ${hashBigInt <= challengeInt}`);

// Also: check wasm_solve with prefix ONLY (no challenge)
// The WASM might internally use challenge for target comparison
// Let me try: maybe the condition is different

// Brute force small range to find what nonce actually satisfies
console.log('\nBrute forcing first 100000 nonces to find SHA3 condition...');
let found = false;
for (let n = 0; n < 200000; n++) {
  const h = deepseekHash(prefix + n);
  const hi = BigInt('0x' + h);
  if (Number(hi % BigInt(difficulty)) === 0) {
    console.log(`  Found via mod: nonce=${n}, hash=${h}`);
    if (n === nonce) console.log('  *** THIS IS THE EXPECTED NONCE! ***');
    if (!found) found = true;
    break;
  }
}
if (!found) {
  // Try with challenge included
  console.log('  Not found via mod. Trying challenge-included hash...');
  for (let n = 0; n < 200000; n++) {
    // variant 1: prefix + nonce (but hash includes challenge somehow?)
    const h1 = deepseekHash(challenge + '_' + prefix + n);
    const h1i = BigInt('0x' + h1);
    if (Number(h1i % BigInt(difficulty)) === 0) {
      console.log(`  Found variant (challenge+prefix+n): nonce=${n}`);
      break;
    }
  }
}
