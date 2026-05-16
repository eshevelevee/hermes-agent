#!/usr/bin/env python3
"""
Hermes READ_ONLY_INTAKE patches for @whiskeysockets/baileys.

Pinned upstream: WhiskeySockets/Baileys#01047debd81beb20da7b7779b08edcb06aa03770
                 (= 7.0.0-rc.9 RC).

Idempotent. Refuses to double-apply via __hermesReadOnlyIntake marker.
Atomic: each anchor is asserted before mutation. Aborts cleanly on miss
without writing the file.

Closes:
  P0-1 (post-construction sock.X = wrap CANNOT block messages-recv.js
        closures that captured sendReceipt, sendNode at construction time).

Patched sites in node_modules/@whiskeysockets/baileys/lib/Socket/messages-recv.js:
  A. Helper function __hermesReadOnlyIntake() inserted in makeMessagesRecvSocket
     scope (after sock construction). Truthy reads from
     WHATSAPP_READ_ONLY_INTAKE env at call time.
  B. Auto-receipt block: wraps the 2 sendReceipt() calls on inbound
     messages (delivery receipt + history-sync receipt). User-visible
     "delivered" double-tick suppressed.
  C. Retry-request: wraps sendNode(receipt) inside sendRetryRequest
     (server-redelivery request — outbound stanza, suppressed under
     read-only).

Run as npm postinstall:
    "postinstall": "python3 scripts/apply_hermes_patches.py"

Run manually:
    cd scripts/whatsapp-bridge && python3 scripts/apply_hermes_patches.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Resolve relative to this script's location:
#   scripts/whatsapp-bridge/scripts/apply_hermes_patches.py
# Target lives at:
#   scripts/whatsapp-bridge/node_modules/@whiskeysockets/baileys/lib/Socket/messages-recv.js
HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent
TARGET = BRIDGE_DIR / 'node_modules' / '@whiskeysockets' / 'baileys' / 'lib' / 'Socket' / 'messages-recv.js'

HELPER = """
function __hermesReadOnlyIntake() {
    const v = String(process.env.WHATSAPP_READ_ONLY_INTAKE || '').toLowerCase();
    return v === '1' || v === 'true' || v === 'yes' || v === 'on';
}
"""

ANCHOR_HELPER = "    const sock = makeMessagesSocket(config);"

ANCHOR_RECEIPT_OLD = """                        acked = true;
                        await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type);
                        // send ack for history message
                        const isAnyHistoryMsg = getHistoryMsg(msg.message);
                        if (isAnyHistoryMsg) {
                            const jid = jidNormalizedUser(msg.key.remoteJid);
                            await sendReceipt(jid, undefined, [msg.key.id], 'hist_sync'); // TODO: investigate
                        }"""

ANCHOR_RECEIPT_NEW = """                        acked = true;
                        if (!__hermesReadOnlyIntake()) {
                            await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type);
                            // send ack for history message
                            const isAnyHistoryMsg = getHistoryMsg(msg.message);
                            if (isAnyHistoryMsg) {
                                const jid = jidNormalizedUser(msg.key.remoteJid);
                                await sendReceipt(jid, undefined, [msg.key.id], 'hist_sync'); // TODO: investigate
                            }
                        }"""

ANCHOR_RETRY_OLD = """            await sendNode(receipt);
            logger.info({ msgAttrs: node.attrs, retryCount }, 'sent retry receipt');"""

ANCHOR_RETRY_NEW = """            if (!__hermesReadOnlyIntake()) {
                await sendNode(receipt);
                logger.info({ msgAttrs: node.attrs, retryCount }, 'sent retry receipt');
            }"""


def main() -> int:
    if not TARGET.exists():
        # Not an error: node_modules may not exist yet (pre-npm-install).
        # postinstall fires AFTER deps install so this should be reached
        # with node_modules present. If missing, log and exit OK so we
        # don't break local dev workflows that don't install bridge deps.
        print(f'SKIP_NO_TARGET: {TARGET}', file=sys.stderr)
        return 0

    content = TARGET.read_text(encoding='utf-8')

    if '__hermesReadOnlyIntake' in content:
        print('SKIP_ALREADY_PATCHED')
        return 0

    # Pre-flight: verify ALL anchors before mutating
    missing = []
    if ANCHOR_HELPER not in content:
        missing.append('helper_anchor')
    if ANCHOR_RECEIPT_OLD not in content:
        missing.append('receipt_anchor')
    if ANCHOR_RETRY_OLD not in content:
        missing.append('retry_anchor')
    if missing:
        print(f'ERR: missing anchors: {missing}', file=sys.stderr)
        print('Baileys upstream may have changed. Re-pin or refresh anchors.', file=sys.stderr)
        return 2

    # Apply (replace count=1 for each)
    content = content.replace(ANCHOR_HELPER, ANCHOR_HELPER + HELPER, 1)
    content = content.replace(ANCHOR_RECEIPT_OLD, ANCHOR_RECEIPT_NEW, 1)
    content = content.replace(ANCHOR_RETRY_OLD, ANCHOR_RETRY_NEW, 1)

    # Post-flight: verify exactly 3 occurrences
    occurrence_count = content.count('__hermesReadOnlyIntake')
    if occurrence_count != 3:
        print(f'ERR: post-apply count={occurrence_count}, expected exactly 3', file=sys.stderr)
        return 3

    TARGET.write_text(content, encoding='utf-8')
    print('OK_PATCHED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
