#!/bin/bash
# wa_intake_kill_switch.sh — kill switch for the READ_ONLY_INTAKE WhatsApp
# pipeline. Idempotent. Safe to run multiple times.
#
# Order is deliberate:
#   1. Stop any wa-intake systemd service (no-op if not installed)
#   2. Kill the Node bridge if it's still alive after step 1
#   3. Delete the WhatsApp session keys (revokes the linked-device session
#      locally — does NOT revoke it on Meta's side)
#   4. Delete cached received media (image / document / audio caches)
#   5. Move the intake DB aside (preserved as audit trail, not deleted)
#
# The intake DB is NEVER auto-deleted: if the kill switch fires because of
# unexpected behavior, the DB is the evidence to investigate. Rename with
# timestamp; SD removes manually after review.
#
# To FULLY revoke the linked-device session at the WhatsApp account level,
# the SD must ALSO open WhatsApp on their iPhone and tap
#   Settings → Linked Devices → Hermes Agent → Log Out
# without that step a new bridge instance could re-pair from any backup of
# the session directory.

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SESSION_DIR="${WHATSAPP_SESSION_PATH:-$HERMES_HOME/whatsapp/session}"
INTAKE_DB="${WHATSAPP_INTAKE_DB:-$HERMES_HOME/whatsapp/intake.db}"

echo "[kill] HERMES_HOME=$HERMES_HOME"
echo "[kill] session_dir=$SESSION_DIR"
echo "[kill] intake_db=$INTAKE_DB"
echo

# 1. Stop systemd unit if present.
echo "[kill] 1/5 stop wa-intake.service (if managed)"
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop wa-intake.service 2>/dev/null || true
fi

# 2. Kill any orphan Node bridge process.
echo "[kill] 2/5 kill orphan bridge.js processes"
pkill -f "scripts/whatsapp-bridge/bridge.js" 2>/dev/null || true
# Brief grace period for OS to release the loopback port.
sleep 1

# 3. Delete session keys (creds.json, pre-keys, lid-mapping-*.json).
echo "[kill] 3/5 delete session keys at $SESSION_DIR"
if [[ -d "$SESSION_DIR" ]]; then
  rm -rf "$SESSION_DIR"
  echo "[kill]    removed."
else
  echo "[kill]    not present (already clean)."
fi

# 4. Delete cached received media.
echo "[kill] 4/5 delete media caches"
for cache in image_cache document_cache audio_cache; do
  CACHE_DIR="$HERMES_HOME/$cache"
  if [[ -d "$CACHE_DIR" ]]; then
    rm -rf "$CACHE_DIR"
    echo "[kill]    removed $cache."
  fi
done

# 5. Move intake DB aside (preserve audit trail).
echo "[kill] 5/5 quarantine intake DB (NOT deleted)"
if [[ -f "$INTAKE_DB" ]]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  mv "$INTAKE_DB" "${INTAKE_DB}.killed.${STAMP}"
  # Also move sidecar WAL / SHM files.
  for sfx in -wal -shm; do
    if [[ -f "${INTAKE_DB}${sfx}" ]]; then
      mv "${INTAKE_DB}${sfx}" "${INTAKE_DB}.killed.${STAMP}${sfx}"
    fi
  done
  echo "[kill]    renamed to ${INTAKE_DB}.killed.${STAMP}"
else
  echo "[kill]    not present (already clean)."
fi

echo
echo "[kill] DONE on this host."
echo
echo "[kill] MANUAL ACTION REQUIRED:"
echo "[kill]   On your iPhone open WhatsApp -> Settings -> Linked Devices"
echo "[kill]   -> 'Hermes Agent' -> Log Out."
echo "[kill]   Without revoking on iPhone, a new bridge instance could re-pair"
echo "[kill]   from a session-key backup."
