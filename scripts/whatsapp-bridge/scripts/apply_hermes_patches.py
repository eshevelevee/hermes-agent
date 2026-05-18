#!/usr/bin/env python3
"""
Hermes READ_ONLY_INTAKE patches for @whiskeysockets/baileys.

Pinned upstream: WhiskeySockets/Baileys#01047debd81beb20da7b7779b08edcb06aa03770
                 (= 7.0.0-rc.9 RC).

Idempotent per-patch. Refuses to double-apply via new-text presence.
Atomic: each anchor is asserted before mutation. Aborts cleanly on miss
without writing the file.

Patched sites:
  messages-recv.js:
    A. Helper function __hermesReadOnlyIntake() inserted in makeMessagesRecvSocket.
    B. Auto-receipt block: wraps sendReceipt() calls on inbound messages.
    C. Retry-request: wraps sendNode(receipt) inside sendRetryRequest.
    D. Retry-relay: wraps relayMessage() inside retry request handler.
  chats.js:
    E. Helper function __hermesReadOnlyIntake() inserted in makeChatsSocket.
    F. sendPresenceUpdate guard: early return under read-only.
    G. Presence auto-fire on connection.open wrapped under read-only.
    H. updateProfileName (pushNameSetting) guard under read-only.

Run as npm postinstall:
    "postinstall": "python3 scripts/apply_hermes_patches.py"

Run manually:
    cd scripts/whatsapp-bridge && python3 scripts/apply_hermes_patches.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent

HELPER = """\nfunction __hermesReadOnlyIntake() {\n    const v = String(process.env.WHATSAPP_READ_ONLY_INTAKE || '').toLowerCase();\n    return v === '1' || v === 'true' || v === 'yes' || v === 'on';\n}\n"""

PATCHES = [
    # ------------------------------------------------------------------ messages-recv.js
    {
        'file': 'messages-recv.js',
        'old': '    const sock = makeMessagesSocket(config);',
        'new': '    const sock = makeMessagesSocket(config);' + HELPER,
    },
    {
        'file': 'messages-recv.js',
        'old': """                        acked = true;
                        await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type);
                        // send ack for history message
                        const isAnyHistoryMsg = getHistoryMsg(msg.message);
                        if (isAnyHistoryMsg) {
                            const jid = jidNormalizedUser(msg.key.remoteJid);
                            await sendReceipt(jid, undefined, [msg.key.id], 'hist_sync'); // TODO: investigate
                        }""",
        'new': """                        acked = true;
                        if (!__hermesReadOnlyIntake()) {
                            await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type);
                            // send ack for history message
                            const isAnyHistoryMsg = getHistoryMsg(msg.message);
                            if (isAnyHistoryMsg) {
                                const jid = jidNormalizedUser(msg.key.remoteJid);
                                await sendReceipt(jid, undefined, [msg.key.id], 'hist_sync'); // TODO: investigate
                            }
                        }""",
    },
    {
        'file': 'messages-recv.js',
        'old': """            await sendNode(receipt);
            logger.info({ msgAttrs: node.attrs, retryCount }, 'sent retry receipt');""",
        'new': """            if (!__hermesReadOnlyIntake()) {
                await sendNode(receipt);
                logger.info({ msgAttrs: node.attrs, retryCount }, 'sent retry receipt');
            }""",
    },
    {
        'file': 'messages-recv.js',
        'old': '                await relayMessage(key.remoteJid, msg, msgRelayOpts);',
        'new': """                if (!__hermesReadOnlyIntake()) {
                    await relayMessage(key.remoteJid, msg, msgRelayOpts);
                }""",
    },
    # ------------------------------------------------------------------ chats.js
    {
        'file': 'chats.js',
        'old': '    const sock = makeSocket(config);',
        'new': '    const sock = makeSocket(config);' + HELPER,
    },
    {
        'file': 'chats.js',
        'old': """    const sendPresenceUpdate = async (type, toJid) => {
        const me = authState.creds.me;""",
        'new': """    const sendPresenceUpdate = async (type, toJid) => {
        if (__hermesReadOnlyIntake()) return;
        const me = authState.creds.me;""",
    },
    {
        'file': 'chats.js',
        'old': '            sendPresenceUpdate(markOnlineOnConnect ? \'available\' : \'unavailable\').catch(error => onUnexpectedError(error, \'presence update requests\'));',
        'new': """            if (!__hermesReadOnlyIntake()) {
                sendPresenceUpdate(markOnlineOnConnect ? 'available' : 'unavailable').catch(error => onUnexpectedError(error, 'presence update requests'));
            }""",
    },
    {
        'file': 'chats.js',
        'old': """    const updateProfileName = async (name) => {
        await chatModify({ pushNameSetting: name }, '');
    };""",
        'new': """    const updateProfileName = async (name) => {
        if (__hermesReadOnlyIntake()) return;
        await chatModify({ pushNameSetting: name }, '');
    };""",
    },
]


def run(base_dir: Path) -> int:
    baileys = base_dir / 'node_modules' / '@whiskeysockets' / 'baileys' / 'lib' / 'Socket'
    all_targets = {p['file'] for p in PATCHES}
    written_any = False

    for target_name in sorted(all_targets):
        target = baileys / target_name
        if not target.exists():
            print(f'SKIP_NO_TARGET: {target}', file=sys.stderr)
            continue

        content = target.read_text(encoding='utf-8')
        patches_for_file = [p for p in PATCHES if p['file'] == target_name]
        modified = False

        for patch in patches_for_file:
            old = patch['old']
            new = patch['new']

            if new in content:
                # Already applied (idempotent)
                continue

            if old not in content:
                print(f'ERR: anchor drift in {target_name}', file=sys.stderr)
                print('Baileys upstream may have changed. Re-pin or refresh anchors.', file=sys.stderr)
                return 2

            content = content.replace(old, new, 1)
            modified = True

        if modified:
            target.write_text(content, encoding='utf-8')
            written_any = True
            print(f'OK_PATCHED: {target_name}')

    if not written_any:
        print('SKIP_ALREADY_PATCHED')

    return 0


def main() -> int:
    if len(sys.argv) > 1:
        base_dir = Path(sys.argv[1]).resolve()
    else:
        base_dir = BRIDGE_DIR
    return run(base_dir)


if __name__ == '__main__':
    sys.exit(main())
