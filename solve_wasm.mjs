#!/usr/bin/env node
/**
 * Usa el WASM de DeepSeek directamente para resolver el PoW.
 * wasm_solve(out_ptr, challenge_ptr, challenge_len, prefix_ptr, prefix_len, difficulty_f64)
 */
import { readFileSync } from 'fs';

const wasmBytes = readFileSync('/Users/cristian/deep/sha3_wasm_bg.wasm');

// wasm-bindgen boilerplate simplificado
let memory, exports;
let cachedTextEncoder = new TextEncoder();
let cachedTextDecoder = new TextDecoder('utf-8', { ignoreBOM: true, fatal: true });
let cachegetUint8Memory0 = null;
function getUint8Memory0() {
  if (cachegetUint8Memory0 === null || cachegetUint8Memory0.buffer !== memory.buffer) {
    cachegetUint8Memory0 = new Uint8Array(memory.buffer);
  }
  return cachegetUint8Memory0;
}

let WASM_VECTOR_LEN = 0;
function passStringToWasm0(arg, malloc, realloc) {
  const buf = cachedTextEncoder.encode(arg);
  const ptr = malloc(buf.length + 1, 1);
  getUint8Memory0().subarray(ptr, ptr + buf.length).set(buf);
  getUint8Memory0()[ptr + buf.length] = 0;
  WASM_VECTOR_LEN = buf.length;
  return ptr;
}

let cachegetInt32Memory0 = null;
function getInt32Memory0() {
  if (cachegetInt32Memory0 === null || cachegetInt32Memory0.buffer !== memory.buffer) {
    cachegetInt32Memory0 = new Int32Array(memory.buffer);
  }
  return cachegetInt32Memory0;
}

let cachegetFloat64Memory0 = null;
function getFloat64Memory0() {
  if (cachegetFloat64Memory0 === null || cachegetFloat64Memory0.buffer !== memory.buffer) {
    cachegetFloat64Memory0 = new Float64Array(memory.buffer);
  }
  return cachegetFloat64Memory0;
}

// Minimal wasm-bindgen runtime
const importObject = {
  __wbindgen_placeholder__: {
    __wbindgen_throw: (ptr, len) => {
      const msg = cachedTextDecoder.decode(getUint8Memory0().subarray(ptr, ptr + len));
      throw new Error(msg);
    },
  },
  // Some versions use this
  wbg: {
    __wbindgen_throw: (ptr, len) => {
      const msg = cachedTextDecoder.decode(getUint8Memory0().subarray(ptr, ptr + len));
      throw new Error(msg);
    },
  }
};

const wasmModule = await WebAssembly.instantiate(wasmBytes, importObject);
exports = wasmModule.instance.exports;
memory = exports.memory;

console.log('WASM exports:', Object.keys(exports));

// Solve function matching the JS worker pattern:
// wasm_solve(ret_ptr, challenge_ptr, challenge_len, prefix_ptr, prefix_len, difficulty)
function wasm_solve(challenge, prefix, difficulty) {
  const malloc = exports.__wbindgen_export_0;
  const realloc = exports.__wbindgen_export_1;

  // Allocate return value area
  const retPtr = exports.__wbindgen_add_to_stack_pointer(-16);

  try {
    // Encode challenge string
    const ptr0 = passStringToWasm0(challenge, malloc, realloc);
    const len0 = WASM_VECTOR_LEN;

    // Encode prefix string
    const ptr1 = passStringToWasm0(prefix, malloc, realloc);
    const len1 = WASM_VECTOR_LEN;

    exports.wasm_solve(retPtr, ptr0, len0, ptr1, len1, difficulty);

    const status = getInt32Memory0()[retPtr / 4 + 0];
    const nonce = getFloat64Memory0()[retPtr / 8 + 1];

    if (status === 0) {
      return undefined; // no solution found
    }
    return nonce;
  } finally {
    exports.__wbindgen_add_to_stack_pointer(16);
  }
}

// Test with the real challenge
const challenge = '41e63c2e79a1429f7fc4c11e5c8142fe37e0d4807a6a8d62b78dfe81777a30d9';
const salt = 'c92d818d0be8c82a42a1';
const expireAt = 1786974074928;
const difficulty = 144000;

// Prefix format from web worker: salt + "_" + expireAt + "_"
const prefix = `${salt}_${expireAt}_`;
console.log(`\nChallenge: ${challenge}`);
console.log(`Prefix: ${prefix}`);
console.log(`Difficulty: ${difficulty}`);
console.log('\nSolving...');

const start = Date.now();
const nonce = wasm_solve(challenge, prefix, difficulty);
const elapsed = Date.now() - start;

console.log(`\nNonce: ${nonce}`);
console.log(`Elapsed: ${elapsed}ms`);
console.log(`Type: ${typeof nonce}`);

if (nonce !== undefined) {
  // Verify: SHA3-256(prefix + nonce) % difficulty == 0 ?
  // We'd need sha3 in node to verify, but the WASM result is the authoritative answer
  console.log('\nPoW solved! Answer (nonce):', nonce);
}
