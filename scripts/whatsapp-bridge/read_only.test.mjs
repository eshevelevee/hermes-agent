// read_only.test.mjs — READ_ONLY_INTAKE coverage for the WhatsApp bridge.
//
// Two test families:
//   1. HTTP middleware (Layer A) — spawn the bridge with
//      WHATSAPP_READ_ONLY_INTAKE=true on an isolated port + temp session,
//      hit each endpoint, assert 403 on the four outbound routes and
//      200 on /health and /messages. The bridge will not connect to
//      WhatsApp (no pairing), but the routes that don't need the socket
//      are reachable.
//   2. Static scanner — read bridge.js source, assert every app.post route
//      is in OUTBOUND_ROUTES, every sock.send* / sock.read* / sock.update*
//      callsite is either wrapped or inside a blocked-route handler, and
//      every downloadMediaMessage callsite goes through safeDownloadOptions.

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, readFileSync, writeFileSync, statSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { setTimeout as wait } from 'node:timers/promises';

const BRIDGE_PATH = path.resolve('bridge.js');

function pickPort() {
  return 30000 + Math.floor(Math.random() * 5000);
}

async function fetchJson(url, { method = 'GET', body, host = 'localhost' } = {}) {
  const init = {
    method,
    headers: { Host: host, 'Content-Type': 'application/json' },
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  const res = await fetch(url, init);
  let parsed;
  try { parsed = await res.json(); } catch { parsed = null; }
  return { status: res.status, body: parsed };
}

async function spawnBridge({ readOnly, port, sessionDir, extraEnv = {} }) {
  const env = {
    ...process.env,
    WHATSAPP_MODE: 'self-chat',
    WHATSAPP_DEBUG: '0',
    ...extraEnv,
  };
  if (readOnly) env.WHATSAPP_READ_ONLY_INTAKE = 'true';
  else delete env.WHATSAPP_READ_ONLY_INTAKE;
  const child = spawn(
    process.execPath,
    [BRIDGE_PATH, '--port', String(port), '--session', sessionDir],
    { env, stdio: ['ignore', 'pipe', 'pipe'] }
  );
  const stdoutChunks = [];
  const stderrChunks = [];
  child.stdout.on('data', (d) => stdoutChunks.push(d.toString()));
  child.stderr.on('data', (d) => stderrChunks.push(d.toString()));

  // Wait until /health is reachable (max 5s).
  const deadline = Date.now() + 5000;
  let healthy = false;
  while (Date.now() < deadline) {
    try {
      const res = await fetchJson(`http://127.0.0.1:${port}/health`);
      if (res.status === 200) { healthy = true; break; }
    } catch {}
    await wait(150);
  }
  return {
    child,
    healthy,
    stdout: () => stdoutChunks.join(''),
    stderr: () => stderrChunks.join(''),
  };
}

function killBridge(handle) {
  try { handle.child.kill('SIGTERM'); } catch {}
}

// --------------------------------------------------------------------- L1 HTTP

test('RO-js-1..7: bridge in READ_ONLY_INTAKE blocks 4 outbound routes, allows 3 inbound', async () => {
  const port = pickPort();
  const sessionDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-ro-'));
  const h = await spawnBridge({ readOnly: true, port, sessionDir });
  try {
    assert.ok(h.healthy, `bridge did not become healthy; stderr=${h.stderr().slice(0, 600)}`);

    const health = await fetchJson(`http://127.0.0.1:${port}/health`);
    assert.equal(health.status, 200, 'RO-js-7: /health should be 200');
    assert.equal(health.body.readOnlyIntake, true, '/health must surface read-only flag');

    const messages = await fetchJson(`http://127.0.0.1:${port}/messages`);
    assert.equal(messages.status, 200, 'RO-js-5: GET /messages should be 200');

    const chatInfo = await fetchJson(`http://127.0.0.1:${port}/chat/x`);
    assert.equal(chatInfo.status, 200,
      'RO-js-6: GET /chat/:id is allowed under read-only (returns fallback for non-group)');

    const send = await fetchJson(`http://127.0.0.1:${port}/send`, {
      method: 'POST', body: { chatId: 'x@s.whatsapp.net', message: 'hi' },
    });
    assert.equal(send.status, 403, 'RO-js-1: POST /send must be 403');
    assert.equal(send.body.error, 'disabled_by_policy', 'RO-js-1: body.error');
    assert.equal(send.body.mode, 'read_only_intake');

    const edit = await fetchJson(`http://127.0.0.1:${port}/edit`, {
      method: 'POST', body: { chatId: 'x', messageId: 'y', message: 'hi' },
    });
    assert.equal(edit.status, 403, 'RO-js-2: POST /edit must be 403');
    assert.equal(edit.body.error, 'disabled_by_policy');

    const sendMedia = await fetchJson(`http://127.0.0.1:${port}/send-media`, {
      method: 'POST', body: { chatId: 'x', filePath: '/dev/null', mediaType: 'image' },
    });
    assert.equal(sendMedia.status, 403, 'RO-js-3: POST /send-media must be 403');

    const typing = await fetchJson(`http://127.0.0.1:${port}/typing`, {
      method: 'POST', body: { chatId: 'x' },
    });
    assert.equal(typing.status, 403, 'RO-js-4: POST /typing must be 403');
  } finally {
    killBridge(h);
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

test('RO-js-8: bridge with READ_ONLY_INTAKE=false returns 503 (not connected) on /send, NOT 403', async () => {
  const port = pickPort();
  const sessionDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-ro-'));
  const h = await spawnBridge({ readOnly: false, port, sessionDir });
  try {
    assert.ok(h.healthy, 'bridge did not become healthy');
    const send = await fetchJson(`http://127.0.0.1:${port}/send`, {
      method: 'POST', body: { chatId: 'x', message: 'y' },
    });
    assert.notEqual(send.status, 403,
      'RO-js-8: middleware must NOT block when read-only flag is false');
    // Expect 503 (not connected to WhatsApp — we didn't pair) or 500.
    assert.ok(send.status === 503 || send.status === 500,
      `RO-js-8: expected 503/500, got ${send.status}`);
  } finally {
    killBridge(h);
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

test('RO-js-19: bridge boot log includes READ_ONLY_INTAKE=true', async () => {
  const port = pickPort();
  const sessionDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-ro-'));
  const h = await spawnBridge({ readOnly: true, port, sessionDir });
  try {
    assert.ok(h.healthy, 'bridge did not become healthy');
    await wait(200);
    const out = h.stdout();
    assert.match(out, /READ_ONLY_INTAKE=true/,
      `boot log must contain "READ_ONLY_INTAKE=true"; got: ${out.slice(0, 400)}`);
  } finally {
    killBridge(h);
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------- L2 wrapper shape

test('RO-js-9..15: readOnlyDeny factory is present, returns async function that throws WA_READ_ONLY_INTAKE', () => {
  // We do NOT eval the function from source (would trip the security hook
  // and add a code-injection vector to the test harness). Instead we rely
  // on (a) source-level structural assertions below and (b) the spawn-based
  // integration test in RO-js-1..7, which exercises the full L1 path.
  // Runtime L2 behavior is validated end-to-end in the Phase-3 sandbox
  // strace test (see spec §Triple Security Review).
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  assert.match(src, /function readOnlyDeny\(methodName\)/,
    'readOnlyDeny factory must be defined');
  assert.match(src, /err\.code\s*=\s*['"]WA_READ_ONLY_INTAKE['"]/,
    'readOnlyDeny must tag thrown error with code WA_READ_ONLY_INTAKE');
  assert.match(src, /BLOCKED sock\.\$\{methodName\}/,
    'readOnlyDeny must log the blocked method name');
});

test('RO-js-15b: READ_ONLY_BAILEYS_BLOCKED_METHODS includes the full outbound set', () => {
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  const required = [
    'sendMessage', 'sendPresenceUpdate', 'sendReceipt', 'readMessages',
    'updateMediaMessage', 'chatModify', 'sendChatModification',
    'groupCreate', 'groupLeave', 'groupUpdateSubject',
    'groupParticipantsUpdate', 'updateBlockStatus',
    'updateProfilePicture', 'updateProfileName', 'updateProfileStatus',
  ];
  for (const m of required) {
    assert.match(src, new RegExp(`'${m}'`),
      `READ_ONLY_BAILEYS_BLOCKED_METHODS must list '${m}'`);
  }
});

test('RO-js-15c: socket-wrap loop is wired into startSocket() under READ_ONLY_INTAKE', () => {
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  assert.match(
    src,
    /if \(READ_ONLY_INTAKE\)\s*\{[\s\S]*?for \(const m of READ_ONLY_BAILEYS_BLOCKED_METHODS\)/,
    'startSocket must wrap each outbound method when READ_ONLY_INTAKE is true'
  );
});

// ------------------------------------------------------------ static scanner

test('RO-js-16: every app.post route is in OUTBOUND_ROUTES (no unguarded POST endpoints)', () => {
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  const postRoutes = [...src.matchAll(/app\.post\(['"]([^'"]+)['"]/g)].map(m => m[1]);
  const outboundLine = src.match(/const OUTBOUND_ROUTES = new Set\(\[([^\]]+)\]\)/);
  assert.ok(outboundLine, 'OUTBOUND_ROUTES constant must exist');
  const declared = [...outboundLine[1].matchAll(/'([^']+)'/g)].map(m => m[1]);
  for (const route of postRoutes) {
    assert.ok(declared.includes(route),
      `RO-js-16: POST ${route} is unguarded — add to OUTBOUND_ROUTES`);
  }
  // Make sure no stray POST route exists outside the declared set.
  assert.deepEqual([...postRoutes].sort(), [...declared].sort(),
    'RO-js-16: bridge.js POST routes must exactly match OUTBOUND_ROUTES');
});

test('RO-js-17: every sock.send/sock.read/sock.update callsite is wrapped or inside a blocked-route handler', () => {
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  const lines = src.split('\n');
  const offenders = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const stripped = line.replace(/\/\/.*$/, '');
    // Audit-derived outbound surface: send/read/update + relay, presence,
    // chat, group, newsletter, reject, product, plus specific peer-data ops.
    const MUTATOR_RE = /\bsock\.(send|read|update|relay|presence|chat|group|newsletter|reject|product|requestPlaceholderResend|fetchMessageHistory|createCallLink|cleanDirtyBits|star\b|addOrEdit|addLabel|addChatLabel|addMessageLabel|removeContact|removeProfilePicture|removeChatLabel|removeMessageLabel|removeQuickReply|removeCoverPhoto)\w*/;
    if (!MUTATOR_RE.test(stripped)) continue;
    if (/sock\[m\]/.test(stripped)) continue; // the wrapper assignment
    if (/READ_ONLY_BAILEYS_BLOCKED_METHODS/.test(stripped)) continue;
    if (/safeDownloadOptions/.test(stripped)) continue; // helper, not a direct call
    // Protocol primitives / explicit reads — NOT mutators.
    if (/\bsock\.sendNode\b/.test(stripped)) continue;
    if (/\bsock\.sendRawMessage\b/.test(stripped)) continue;
    if (/\bsock\.sendMessageAck\b/.test(stripped)) continue;
    if (/\bsock\.groupMetadata\b/.test(stripped)) continue;
    // Hits inside app.post('/send'/'/edit'/'/send-media'/'/typing') handlers
    // are OK because Layer A blocks them before they execute.
    let allowed = false;
    for (let j = i - 1; j >= Math.max(0, i - 120); j -= 1) {
      const prev = lines[j];
      const m = prev.match(/app\.post\(['"](\/send|\/edit|\/send-media|\/typing)['"]/);
      if (m) { allowed = true; break; }
      if (/^app\.(get|post|use)\(/.test(prev)) break;
    }
    if (!allowed) {
      offenders.push({ line: i + 1, text: stripped.trim() });
    }
  }
  assert.deepEqual(offenders, [],
    `RO-js-17: unguarded sock outbound callsite(s): ${JSON.stringify(offenders, null, 2)}`);
});

test('RO-js-18: every downloadMediaMessage callsite goes through safeDownloadOptions', () => {
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  // Drop the import line; only inspect actual call expressions.
  const stripped = src.replace(/import[^;]+downloadMediaMessage[^;]+;/g, '');
  const callsites = [...stripped.matchAll(/downloadMediaMessage\s*\(([^)]+)\)/g)].map(m => m[0]);
  assert.ok(callsites.length >= 4,
    `RO-js-18: expected 4 downloadMediaMessage callsites, found ${callsites.length}`);
  for (const call of callsites) {
    assert.match(call, /safeDownloadOptions\s*\(\s*sock\s*\)/,
      `RO-js-18: callsite must use safeDownloadOptions(sock): ${call}`);
  }
});

// ------------------------------------------------------------ regression

test('RO-js-20: existing allowlist.js exports are intact (regression)', () => {
  const allowlistSrc = readFileSync(path.resolve('allowlist.js'), 'utf8');
  for (const ident of [
    'matchesAllowedUser', 'parseAllowedUsers',
    'normalizeWhatsAppIdentifier', 'expandWhatsAppIdentifiers',
  ]) {
    assert.match(allowlistSrc, new RegExp(`export function ${ident}\\b`),
      `allowlist.js must still export ${ident}`);
  }
});

// ------------------------------------------------------- RED-fix patch coverage

test('RO-js-21: Baileys messages-recv.js carries __hermesReadOnlyIntake source patches', () => {
  // Closes RED finding #1: post-construction sock.X = readOnlyDeny CANNOT
  // block closures captured at construction time inside messages-recv.js.
  // Source patches inject env-flag guards at the offending callsites.
  // Run scripts/apply_hermes_patches.py if this test fails — it's idempotent.
  const baileysPath = path.resolve(
    'node_modules/@whiskeysockets/baileys/lib/Socket/messages-recv.js'
  );
  const src = readFileSync(baileysPath, 'utf8');

  assert.match(
    src,
    /function __hermesReadOnlyIntake\(\)\s*\{/,
    'Baileys must carry the __hermesReadOnlyIntake helper (run scripts/apply_hermes_patches.py)'
  );

  // Helper reads env at call time + returns boolean
  assert.match(
    src,
    /process\.env\.WHATSAPP_READ_ONLY_INTAKE/,
    'helper must read WHATSAPP_READ_ONLY_INTAKE env'
  );

  // Auto-receipt guard (inbound message handler)
  assert.match(
    src,
    /if \(!__hermesReadOnlyIntake\(\)\)\s*\{\s*\n\s*await sendReceipt/,
    'auto-receipt guard must wrap sendReceipt at messages-recv inbound handler'
  );

  // Retry-receipt guard (sendRetryRequest)
  assert.match(
    src,
    /if \(!__hermesReadOnlyIntake\(\)\)\s*\{\s*\n\s*await sendNode\(receipt\)/,
    'retry-receipt guard must wrap sendNode(receipt) in sendRetryRequest'
  );

  // Retry-relay guard (recv retry request handler)
  assert.match(
    src,
    /if \(!__hermesReadOnlyIntake\(\)\)\s*\{\s*\n\s*await relayMessage\(key\.remoteJid, msg, msgRelayOpts\)/,
    'retry-relay guard must wrap relayMessage in retry request handler'
  );

  // Exactly 4 marker occurrences (1 def + 1 receipt-guard + 1 retry-guard + 1 relay-guard)
  const occurrences = (src.match(/__hermesReadOnlyIntake/g) || []).length;
  assert.equal(
    occurrences,
    4,
    `expected exactly 4 __hermesReadOnlyIntake occurrences, found ${occurrences}`
  );
});

test('RO-js-22: READ_ONLY_BAILEYS_BLOCKED_METHODS comprehensively blocks audit-derived mutators', () => {
  // Closes RED finding #2: original 18-entry list missed ~30 mutators
  // (relayMessage, presenceSubscribe, sendRetryRequest, fetchMessageHistory,
  // requestPlaceholderResend, rejectCall, newsletter*, product*, privacy*,
  // contact/label/star, etc.).
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  const required = [
    // messages-send.js
    'sendMessage', 'sendReceipt', 'sendReceipts', 'readMessages',
    'relayMessage', 'sendPeerDataOperationMessage', 'updateMediaMessage',
    // messages-recv.js public
    'sendRetryRequest', 'rejectCall',
    'fetchMessageHistory', 'requestPlaceholderResend',
    // chats.js
    'sendPresenceUpdate', 'presenceSubscribe', 'chatModify',
    'updateProfilePicture', 'updateProfileName', 'updateProfileStatus',
    'updateBlockStatus',
    'updateDisableLinkPreviewsPrivacy', 'updateCallPrivacy',
    'updateMessagesPrivacy', 'updateReadReceiptsPrivacy',
    'updateGroupsAddPrivacy', 'updateDefaultDisappearingMode',
    'addOrEditContact', 'removeContact',
    'addLabel', 'star',
    // groups.js
    'groupCreate', 'groupLeave', 'groupParticipantsUpdate',
    'groupUpdateDescription', 'groupRevokeInvite', 'groupAcceptInvite',
    // newsletter.js
    'newsletterCreate', 'newsletterUpdate', 'newsletterReactMessage',
    'newsletterMute', 'newsletterUnmute', 'newsletterFollow',
    'newsletterUnfollow',
    // business.js
    'productCreate', 'productDelete', 'productUpdate',
  ];
  for (const m of required) {
    assert.match(
      src,
      new RegExp(`'${m}'`),
      `READ_ONLY_BAILEYS_BLOCKED_METHODS must list '${m}'`
    );
  }
});

test('RO-js-22b: protocol-required methods MUST NOT be in BLOCKED_METHODS', () => {
  // Wrapping these would break protocol-level ack of received messages.
  // WhatsApp servers disconnect clients that fail to ack received stanzas.
  const src = readFileSync(BRIDGE_PATH, 'utf8');
  const blockedStart = src.indexOf('const READ_ONLY_BAILEYS_BLOCKED_METHODS');
  assert.notEqual(blockedStart, -1, 'BLOCKED_METHODS const must exist');
  const blockedEnd = src.indexOf('];', blockedStart);
  assert.notEqual(blockedEnd, -1, 'BLOCKED_METHODS array must terminate');
  const arr = src.slice(blockedStart, blockedEnd);

  for (const protoMethod of ['sendMessageAck', 'sendNode', 'sendRawMessage']) {
    assert.equal(
      arr.includes(`'${protoMethod}'`),
      false,
      `${protoMethod} MUST NOT be wrapped (protocol-required; WA disconnects without it)`
    );
  }
});

test('RO-js-23: hardenSessionPerms helper is defined and wired on init + creds.update', () => {
  // Closes RED finding #3: session-key dir created with default umask;
  // saveCreds() didn't chmod files. After this fix, dirs are 0o700 and
  // files are 0o600 after every creds.update.
  const src = readFileSync(BRIDGE_PATH, 'utf8');

  assert.match(
    src,
    /function hardenSessionPerms\(dir\)\s*\{/,
    'hardenSessionPerms helper must be defined'
  );

  assert.match(
    src,
    /chmodSync.*from\s+['"]fs['"]/,
    'chmodSync must be imported from fs'
  );

  assert.match(
    src,
    /chmodSync\(dir,\s*0o700\)/,
    'must chmod 0o700 on directory entries'
  );

  assert.match(
    src,
    /chmodSync\(full,\s*0o600\)/,
    'must chmod 0o600 on file entries'
  );

  assert.match(
    src,
    /mkdirSync\(SESSION_DIR[\s\S]{0,3000}?hardenSessionPerms\(SESSION_DIR\);/,
    'hardenSessionPerms(SESSION_DIR) must be called after mkdirSync init'
  );

  assert.match(
    src,
    /creds\.update[\s\S]{0,500}?await saveCreds\(\);[\s\S]{0,100}?hardenSessionPerms\(SESSION_DIR\);/,
    'creds.update handler must await saveCreds() then call hardenSessionPerms(SESSION_DIR)'
  );
});

test('RO-js-24: bridge boot logs wrapped/missing summary under READ_ONLY_INTAKE', async () => {
  // Verifies the per-method wrap audit log fires once startSocket() reaches
  // the wrap loop (after useMultiFileAuthState + fetchLatestBaileysVersion +
  // makeWASocket — several seconds on fresh tmp session). Poll up to 8s.
  const port = pickPort();
  const sessionDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-ro-'));
  const h = await spawnBridge({ readOnly: true, port, sessionDir });
  try {
    assert.ok(h.healthy, 'bridge did not become healthy');
    const WRAP_RE = /🔒 READ_ONLY: wrapped READ_ONLY_BAILEYS_BLOCKED_METHODS = \d+\/\d+/;
    const deadline = Date.now() + 8000;
    let matched = false;
    while (Date.now() < deadline) {
      if (WRAP_RE.test(h.stdout())) { matched = true; break; }
      await wait(200);
    }
    assert.ok(
      matched,
      `bridge boot must log wrap summary within 8s; stdout=${h.stdout().slice(0, 1000)}`
    );
  } finally {
    killBridge(h);
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

test('RO-js-25: hardenSessionPerms chmods session dir to 0o700 + pre-existing files to 0o600 on init', async () => {
  // Runtime verification of Phase 3 (RED finding #3): not only must
  // hardenSessionPerms exist in source (covered by RO-js-23), it must
  // ACTUALLY chmod on bridge boot. Set initial perms loose (0o755 dir,
  // 0o644 file) and verify they get tightened.
  const port = pickPort();
  const sessionDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-ro-perms-'));
  // mkdtempSync gives 0o700 on macOS, varies on Linux — force loose
  chmodSync(sessionDir, 0o755);
  const fakeFile = path.join(sessionDir, 'fake_creds.json');
  writeFileSync(fakeFile, '{"placeholder": true}');
  chmodSync(fakeFile, 0o644);

  const h = await spawnBridge({ readOnly: true, port, sessionDir });
  try {
    assert.ok(h.healthy, 'bridge did not become healthy');
    // Allow init chmod to run (synchronous after mkdirSync, but spawn
    // process startup needs a tick)
    await wait(500);

    const dirMode = statSync(sessionDir).mode & 0o777;
    assert.equal(
      dirMode,
      0o700,
      `session dir must be 0o700 after init, got 0o${dirMode.toString(8)}`
    );

    const fileMode = statSync(fakeFile).mode & 0o777;
    assert.equal(
      fileMode,
      0o600,
      `pre-existing session file must be 0o600 after init, got 0o${fileMode.toString(8)}`
    );
  } finally {
    killBridge(h);
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

test('RO-js-26: Baileys chats.js carries __hermesReadOnlyIntake source patches', () => {
  const baileysPath = path.resolve(
    'node_modules/@whiskeysockets/baileys/lib/Socket/chats.js'
  );
  const src = readFileSync(baileysPath, 'utf8');

  assert.match(
    src,
    /function __hermesReadOnlyIntake\(\)\s*\{/,
    'chats.js must carry the __hermesReadOnlyIntake helper'
  );

  // sendPresenceUpdate early-return guard
  assert.match(
    src,
    /const sendPresenceUpdate = async \(type, toJid\) => \{\n\s*if \(__hermesReadOnlyIntake\(\)\) return;/,
    'sendPresenceUpdate must have early-return guard under read-only'
  );

  // Presence auto-fire on connection.open wrapped
  assert.match(
    src,
    /if \(!__hermesReadOnlyIntake\(\)\)\s*\{\s*\n\s*sendPresenceUpdate\(markOnlineOnConnect \? 'available' : 'unavailable'\)/,
    'presence auto-fire on connection.open must be wrapped under read-only'
  );

  // updateProfileName (pushNameSetting) guard
  assert.match(
    src,
    /const updateProfileName = async \(name\) => \{\n\s*if \(__hermesReadOnlyIntake\(\)\) return;\n\s*await chatModify\(\{ pushNameSetting: name \}, ''\);/,
    'updateProfileName must have early-return guard under read-only'
  );

  const occurrences = (src.match(/__hermesReadOnlyIntake/g) || []).length;
  assert.equal(
    occurrences,
    4,
    `expected exactly 4 __hermesReadOnlyIntake occurrences in chats.js, found ${occurrences}`
  );
});

test('RO-js-27: patcher fails loud on anchor drift', async () => {
  // Fixture-based drift test: copy clean target, mangle anchor, run patcher,
  // assert non-zero exit and clear error.
  const { execSync } = await import('child_process');
  const { mkdtempSync, writeFileSync, copyFileSync, readFileSync } = await import('fs');
  const { tmpdir } = await import('os');
  const { join } = await import('path');

  const cleanSrc = path.resolve(
    'node_modules/@whiskeysockets/baileys/lib/Socket/messages-recv.js'
  );
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-wa-drift-'));
  const fakeBase = join(tempDir, 'bridge');
  const fakeSocket = join(fakeBase, 'node_modules', '@whiskeysockets', 'baileys', 'lib', 'Socket');
  const tempFile = join(tempDir, 'messages-recv.js');
  copyFileSync(cleanSrc, tempFile);

  // Unapply the helper patch so we have a clean anchor to mangle
  let content = readFileSync(tempFile, 'utf8');
  const helperBlock = 'function __hermesReadOnlyIntake() {\n' +
    '    const v = String(process.env.WHATSAPP_READ_ONLY_INTAKE || \'\').toLowerCase();\n' +
    '    return v === \'1\' || v === \'true\' || v === \'yes\' || v === \'on\';\n' +
    '}\n';
  content = content.replace(helperBlock, '');
  // Mangle the anchor
  content = content.replace(
    '    const sock = makeMessagesSocket(config);',
    '    const sock = makeMessagesSocket(configX);'
  );
  writeFileSync(tempFile, content);

  // Create fake dir structure and copy other files
  const fs = await import('fs');
  fs.mkdirSync(fakeSocket, { recursive: true });
  fs.copyFileSync(tempFile, join(fakeSocket, 'messages-recv.js'));
  fs.copyFileSync(
    path.resolve('node_modules/@whiskeysockets/baileys/lib/Socket/chats.js'),
    join(fakeSocket, 'chats.js')
  );

  let exitCode = 0;
  let stderr = '';
  try {
    execSync(`python3 ${path.resolve('scripts/apply_hermes_patches.py')} "${fakeBase}"`, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (e) {
    exitCode = e.status || 1;
    stderr = e.stderr || '';
  }

  assert.notEqual(exitCode, 0, 'patcher must exit non-zero on anchor drift');
  assert.ok(
    stderr.includes('anchor drift') || stderr.includes('anchor drift'),
    `stderr must mention anchor drift; got: ${stderr.slice(0, 200)}`
  );
});
