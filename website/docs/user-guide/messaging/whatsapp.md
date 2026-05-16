---
sidebar_position: 5
title: "WhatsApp"
description: "Set up Hermes Agent as a WhatsApp bot via the built-in Baileys bridge"
---

# WhatsApp Setup

Hermes connects to WhatsApp through a built-in bridge based on **Baileys**. This works by emulating a WhatsApp Web session — **not** through the official WhatsApp Business API. No Meta developer account or Business verification is required.

:::warning Unofficial API — Ban Risk
WhatsApp does **not** officially support third-party bots outside the Business API. Using a third-party bridge carries a small risk of account restrictions. To minimize risk:
- **Use a dedicated phone number** for the bot (not your personal number)
- **Don't send bulk/spam messages** — keep usage conversational
- **Don't automate outbound messaging** to people who haven't messaged first
:::

:::warning WhatsApp Web Protocol Updates
WhatsApp periodically updates their Web protocol, which can temporarily break compatibility
with third-party bridges. When this happens, Hermes will update the bridge dependency. If the
bot stops working after a WhatsApp update, pull the latest Hermes version and re-pair.
:::

## READ_ONLY_INTAKE Mode (security-hardened pilot)

For deployments where you want WhatsApp **passive ingest only** — no auto-reply, no agent loop, no outbound messages of any kind — set:

```bash
WHATSAPP_READ_ONLY_INTAKE=true
```

When this flag is `true`, three independent layers block every outbound WhatsApp operation:

| Layer | What it stops |
|---|---|
| **HTTP middleware** | `POST /send`, `POST /edit`, `POST /send-media`, `POST /typing` return `403 disabled_by_policy`. `GET /health`, `GET /messages`, `GET /chat/:id` remain available. |
| **Baileys socket wrap** | `sock.sendMessage`, `sendPresenceUpdate`, `sendReceipt`, `readMessages`, `sendReaction`, `updateMediaMessage`, group/profile mutations — all throw `WA_READ_ONLY_INTAKE` if called from inside the bridge. |
| **Python adapter** | Every `send_*` / `edit_message` / `send_typing` on `WhatsAppAdapter` early-returns `SendResult(success=False, error="disabled_by_policy:read_only_intake")` before any HTTP call. |

Incoming messages still flow through `messages.upsert` and are **persisted to a Hermes-local SQLite staging DB** at `~/.hermes/whatsapp/intake.db`. The Hermes agent loop (`handle_message()`) is **suppressed** — no LLM is invoked, no reply is generated, no downstream platform fires. Inspect what was captured with:

```bash
sqlite3 ~/.hermes/whatsapp/intake.db \
  "SELECT id, chat_id, sender_name, body, ingested_at FROM whatsapp_ingest_messages ORDER BY id DESC LIMIT 20;"
```

### Bounded history

| Policy | Default | Env var |
|---|---|---|
| Per-chat row retention | 500 messages | `WHATSAPP_INTAKE_PER_CHAT_LIMIT` |
| Global age retention | 30 days | `WHATSAPP_INTAKE_RETENTION_DAYS` |
| Body length cap | 4096 chars (truncated with `…[truncated]` marker) | `WHATSAPP_INTAKE_BODY_MAX` |
| Media file retention (cache dirs) | 7 days | `WHATSAPP_INTAKE_MEDIA_RETENTION_DAYS` |

Media bytes are **never** stored in the DB — only the path, mime, size, and SHA-256. Voice notes and documents are captured as metadata only; no Whisper / OCR / LLM extraction runs automatically.

### Kill switch

`tools/wa_intake_kill_switch.sh` stops the bridge, wipes the session keys, clears media caches, and quarantines the intake DB (renames with timestamp; does not delete the audit trail). **Manual step required:** open WhatsApp on your iPhone → Settings → Linked Devices → "Hermes Agent" → Log Out. Local cleanup does not revoke the linked-device session on WhatsApp's side.

```bash
bash tools/wa_intake_kill_switch.sh
```

### When to use

- SD-grade WhatsApp intake where the connector must not have any outbound side effect against a personal account
- Compliance-driven setups that need an auditable "we only ever read" guarantee
- Pilots / forensics where the agent loop is intentionally off

### When NOT to use

- Production bot deployments — leave `WHATSAPP_READ_ONLY_INTAKE` unset / `false` and the adapter behaves normally
- Tests that exercise outbound flows — use a mock adapter, not this mode

See `environments/wa-intake/.env.example` for a complete sample env.

---

## Two Modes

| Mode | How it works | Best for |
|------|-------------|----------|
| **Separate bot number** (recommended) | Dedicate a phone number to the bot. People message that number directly. | Clean UX, multiple users, lower ban risk |
| **Personal self-chat** | Use your own WhatsApp. You message yourself to talk to the agent. | Quick setup, single user, testing |

---

## Prerequisites

- **Node.js v18+** and **npm** — the WhatsApp bridge runs as a Node.js process
- **A phone with WhatsApp** installed (for scanning the QR code)

Unlike older browser-driven bridges, the current Baileys-based bridge does **not** require a local Chromium or Puppeteer dependency stack.

---

## Step 1: Run the Setup Wizard

```bash
hermes whatsapp
```

The wizard will:

1. Ask which mode you want (**bot** or **self-chat**)
2. Install bridge dependencies if needed
3. Display a **QR code** in your terminal
4. Wait for you to scan it

**To scan the QR code:**

1. Open WhatsApp on your phone
2. Go to **Settings → Linked Devices**
3. Tap **Link a Device**
4. Point your camera at the terminal QR code

Once paired, the wizard confirms the connection and exits. Your session is saved automatically.

:::tip
If the QR code looks garbled, make sure your terminal is at least 60 columns wide and supports
Unicode. You can also try a different terminal emulator.
:::

---

## Step 2: Getting a Second Phone Number (Bot Mode)

For bot mode, you need a phone number that isn't already registered with WhatsApp. Three options:

| Option | Cost | Notes |
|--------|------|-------|
| **Google Voice** | Free | US only. Get a number at [voice.google.com](https://voice.google.com). Verify WhatsApp via SMS through the Google Voice app. |
| **Prepaid SIM** | $5–15 one-time | Any carrier. Activate, verify WhatsApp, then the SIM can sit in a drawer. Number must stay active (make a call every 90 days). |
| **VoIP services** | Free–$5/month | TextNow, TextFree, or similar. Some VoIP numbers are blocked by WhatsApp — try a few if the first doesn't work. |

After getting the number:

1. Install WhatsApp on a phone (or use WhatsApp Business app with dual-SIM)
2. Register the new number with WhatsApp
3. Run `hermes whatsapp` and scan the QR code from that WhatsApp account

---

## Step 3: Configure Hermes

Add the following to your `~/.hermes/.env` file:

```bash
# Required
WHATSAPP_ENABLED=true
WHATSAPP_MODE=bot                          # "bot" or "self-chat"

# Access control — pick ONE of these options:
WHATSAPP_ALLOWED_USERS=15551234567         # Comma-separated phone numbers (with country code, no +)
# WHATSAPP_ALLOWED_USERS=*                 # OR use * to allow everyone
# WHATSAPP_ALLOW_ALL_USERS=true            # OR set this flag instead (same effect as *)
```

:::tip Allow-all shorthand
Setting `WHATSAPP_ALLOWED_USERS=*` allows **all** senders (equivalent to `WHATSAPP_ALLOW_ALL_USERS=true`).
This is consistent with [Signal group allowlists](/docs/reference/environment-variables).
To use the pairing flow instead, remove both variables and rely on the
[DM pairing system](/docs/user-guide/security#dm-pairing-system).
:::

Optional behavior settings in `~/.hermes/config.yaml`:

```yaml
unauthorized_dm_behavior: pair

whatsapp:
  unauthorized_dm_behavior: ignore
```

- `unauthorized_dm_behavior: pair` is the global default. Unknown DM senders get a pairing code.
- `whatsapp.unauthorized_dm_behavior: ignore` makes WhatsApp stay silent for unauthorized DMs, which is usually the better choice for a private number.

Then start the gateway:

```bash
hermes gateway              # Foreground
hermes gateway install      # Install as a user service
sudo hermes gateway install --system   # Linux only: boot-time system service
```

The gateway starts the WhatsApp bridge automatically using the saved session.

---

## Session Persistence

The Baileys bridge saves its session under `~/.hermes/platforms/whatsapp/session`. This means:

- **Sessions survive restarts** — you don't need to re-scan the QR code every time
- The session data includes encryption keys and device credentials
- **Do not share or commit this session directory** — it grants full access to the WhatsApp account

---

## Re-pairing

If the session breaks (phone reset, WhatsApp update, manually unlinked), you'll see connection
errors in the gateway logs. To fix it:

```bash
hermes whatsapp
```

This generates a fresh QR code. Scan it again and the session is re-established. The gateway
handles **temporary** disconnections (network blips, phone going offline briefly) automatically
with reconnection logic.

---

## Voice Messages

Hermes supports voice on WhatsApp:

- **Incoming:** Voice messages (`.ogg` opus) are automatically transcribed using the configured STT provider: local `faster-whisper`, Groq Whisper (`GROQ_API_KEY`), or OpenAI Whisper (`VOICE_TOOLS_OPENAI_KEY`)
- **Outgoing:** TTS responses are sent as MP3 audio file attachments
- Agent responses are prefixed with "⚕ **Hermes Agent**" by default. You can customize or disable this in `config.yaml`:

```yaml
# ~/.hermes/config.yaml
whatsapp:
  reply_prefix: ""                          # Empty string disables the header
  # reply_prefix: "🤖 *My Bot*\n──────\n"  # Custom prefix (supports \n for newlines)
```

---

## Message Formatting & Delivery

WhatsApp supports **streaming (progressive) responses** — the bot edits its message in real-time as the AI generates text, just like Discord and Telegram. Internally, WhatsApp is classified as a TIER_MEDIUM platform for delivery capabilities.

### Chunking

Long responses are automatically split into multiple messages at **4,096 characters** per chunk (WhatsApp's practical display limit). You don't need to configure anything — the gateway handles splitting and sends chunks sequentially.

### WhatsApp-Compatible Markdown

Standard Markdown in AI responses is automatically converted to WhatsApp's native formatting:

| Markdown | WhatsApp | Renders as |
|----------|----------|------------|
| `**bold**` | `*bold*` | **bold** |
| `~~strikethrough~~` | `~strikethrough~` | ~~strikethrough~~ |
| `# Heading` | `*Heading*` | Bold text (no native headings) |
| `[link text](url)` | `link text (url)` | Inline URL |

Code blocks and inline code are preserved as-is since WhatsApp supports triple-backtick formatting natively.

### Tool Progress

When the agent calls tools (web search, file operations, etc.), WhatsApp displays real-time progress indicators showing which tool is running. This is enabled by default — no configuration needed.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **QR code not scanning** | Ensure terminal is wide enough (60+ columns). Try a different terminal. Make sure you're scanning from the correct WhatsApp account (bot number, not personal). |
| **QR code expires** | QR codes refresh every ~20 seconds. If it times out, restart `hermes whatsapp`. |
| **Session not persisting** | Check that `~/.hermes/platforms/whatsapp/session` exists and is writable. If containerized, mount it as a persistent volume. |
| **Logged out unexpectedly** | WhatsApp unlinks devices after long inactivity. Keep the phone on and connected to the network, then re-pair with `hermes whatsapp` if needed. |
| **Bridge crashes or reconnect loops** | Restart the gateway, update Hermes, and re-pair if the session was invalidated by a WhatsApp protocol change. |
| **Bot stops working after WhatsApp update** | Update Hermes to get the latest bridge version, then re-pair. |
| **macOS: "Node.js not installed" but node works in terminal** | launchd services don't inherit your shell PATH. Run `hermes gateway install` to re-snapshot your current PATH into the plist, then `hermes gateway start`. See the [Gateway Service docs](./index.md#macos-launchd) for details. |
| **Messages not being received** | Verify `WHATSAPP_ALLOWED_USERS` includes the sender's number (with country code, no `+` or spaces), or set it to `*` to allow everyone. Set `WHATSAPP_DEBUG=true` in `.env` and restart the gateway to see raw message events in `bridge.log`. |
| **Bot replies to strangers with a pairing code** | Set `whatsapp.unauthorized_dm_behavior: ignore` in `~/.hermes/config.yaml` if you want unauthorized DMs to be silently ignored instead. |

---

## Security

:::warning
**Configure access control** before going live. Set `WHATSAPP_ALLOWED_USERS` with specific
phone numbers (including country code, without the `+`), use `*` to allow everyone, or set
`WHATSAPP_ALLOW_ALL_USERS=true`. Without any of these, the gateway **denies all incoming
messages** as a safety measure.
:::

By default, unauthorized DMs still receive a pairing code reply. If you want a private WhatsApp number to stay completely silent to strangers, set:

```yaml
whatsapp:
  unauthorized_dm_behavior: ignore
```

- The `~/.hermes/platforms/whatsapp/session` directory contains full session credentials — protect it like a password
- Set file permissions: `chmod 700 ~/.hermes/platforms/whatsapp/session`
- Use a **dedicated phone number** for the bot to isolate risk from your personal account
- If you suspect compromise, unlink the device from WhatsApp → Settings → Linked Devices
- Phone numbers in logs are partially redacted, but review your log retention policy
