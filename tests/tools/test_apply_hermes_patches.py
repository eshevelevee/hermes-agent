"""Tests for apply_hermes_patches.py — drift + idempotency + coverage."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCHER = REPO_ROOT / "scripts" / "whatsapp-bridge" / "scripts" / "apply_hermes_patches.py"
REAL_SOCKET = (
    REPO_ROOT
    / "scripts"
    / "whatsapp-bridge"
    / "node_modules"
    / "@whiskeysockets"
    / "baileys"
    / "lib"
    / "Socket"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("apply_hermes_patches", PATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def patches_mod():
    return _load_module()


@pytest.fixture
def clean_socket_dir(tmp_path, patches_mod):
    """Provide a temp Baileys Socket dir with patches unapplied (clean)."""
    socket_dir = tmp_path / "Socket"
    socket_dir.mkdir(parents=True)

    for target_name in {p["file"] for p in patches_mod.PATCHES}:
        src = REAL_SOCKET / target_name
        dst = socket_dir / target_name
        shutil.copy2(src, dst)
        # Reverse-apply all patches for this file to get a clean copy
        content = dst.read_text(encoding="utf-8")
        file_patches = [p for p in patches_mod.PATCHES if p["file"] == target_name]
        for patch in file_patches:
            if patch["new"] in content:
                content = content.replace(patch["new"], patch["old"], 1)
        dst.write_text(content, encoding="utf-8")

    # Build fake node_modules tree so patcher can resolve paths
    fake_base = tmp_path / "bridge"
    fake_socket = (
        fake_base
        / "node_modules"
        / "@whiskeysockets"
        / "baileys"
        / "lib"
        / "Socket"
    )
    fake_socket.mkdir(parents=True)
    for f in socket_dir.iterdir():
        shutil.copy2(f, fake_socket / f.name)

    return fake_base


def test_patcher_applies_clean(clean_socket_dir):
    """Happy path: patcher succeeds on clean Baileys source."""
    result = subprocess.run(
        [sys.executable, str(PATCHER), str(clean_socket_dir)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OK_PATCHED" in result.stdout


def test_patcher_idempotent(clean_socket_dir):
    """Running twice on already-patched source yields SKIP_ALREADY_PATCHED."""
    # First run
    r1 = subprocess.run(
        [sys.executable, str(PATCHER), str(clean_socket_dir)],
        capture_output=True,
        text=True,
    )
    assert r1.returncode == 0
    # Second run
    r2 = subprocess.run(
        [sys.executable, str(PATCHER), str(clean_socket_dir)],
        capture_output=True,
        text=True,
    )
    assert r2.returncode == 0
    assert "SKIP_ALREADY_PATCHED" in r2.stdout


def test_patcher_fails_loud_on_anchor_drift(clean_socket_dir, patches_mod):
    """Fixture-based drift test: mangle anchor → non-zero exit + clear error."""
    # Pick messages-recv.js as the victim
    target = (
        clean_socket_dir
        / "node_modules"
        / "@whiskeysockets"
        / "baileys"
        / "lib"
        / "Socket"
        / "messages-recv.js"
    )
    content = target.read_text(encoding="utf-8")
    # Mangle the first anchor we can find (helper anchor)
    assert "    const sock = makeMessagesSocket(config);" in content
    mangled = content.replace(
        "    const sock = makeMessagesSocket(config);",
        "    const sock = makeMessagesSocket(configX);",
        1,
    )
    target.write_text(mangled, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PATCHER), str(clean_socket_dir)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"Expected non-zero exit, got {result.returncode}"
    assert "anchor drift" in result.stderr.lower() or "anchor drift" in result.stdout.lower()


def test_patched_chats_js_has_markers(patches_mod, clean_socket_dir):
    """After patch, chats.js carries all expected guards."""
    subprocess.run(
        [sys.executable, str(PATCHER), str(clean_socket_dir)],
        capture_output=True,
        text=True,
    )
    target = (
        clean_socket_dir
        / "node_modules"
        / "@whiskeysockets"
        / "baileys"
        / "lib"
        / "Socket"
        / "chats.js"
    )
    src = target.read_text(encoding="utf-8")
    # Helper
    assert "function __hermesReadOnlyIntake()" in src
    # sendPresenceUpdate guard
    assert "if (__hermesReadOnlyIntake()) return;\n        const me = authState.creds.me;" in src
    # auto-fire guard
    assert "if (!__hermesReadOnlyIntake()) {\n                sendPresenceUpdate(markOnlineOnConnect ? 'available' : 'unavailable')" in src
    # updateProfileName guard
    assert "const updateProfileName = async (name) => {\n        if (__hermesReadOnlyIntake()) return;\n        await chatModify({ pushNameSetting: name }, '');" in src


def test_patched_messages_recv_js_has_markers(patches_mod, clean_socket_dir):
    """After patch, messages-recv.js carries all expected guards."""
    subprocess.run(
        [sys.executable, str(PATCHER), str(clean_socket_dir)],
        capture_output=True,
        text=True,
    )
    target = (
        clean_socket_dir
        / "node_modules"
        / "@whiskeysockets"
        / "baileys"
        / "lib"
        / "Socket"
        / "messages-recv.js"
    )
    src = target.read_text(encoding="utf-8")
    # Helper
    assert "function __hermesReadOnlyIntake()" in src
    # receipt guard
    assert "if (!__hermesReadOnlyIntake()) {\n                            await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type);" in src
    # retry sendNode guard
    assert "if (!__hermesReadOnlyIntake()) {\n                await sendNode(receipt);" in src
    # relayMessage guard
    assert "if (!__hermesReadOnlyIntake()) {\n                    await relayMessage(key.remoteJid, msg, msgRelayOpts);" in src


def test_push_name_setting_not_auto_fired_in_baileys_source(clean_socket_dir, patches_mod):
    """Proof of non-reachability: updateProfileName / pushNameSetting has no internal
    auto-fire callers inside the Baileys source tree."""
    # We scan the *clean* chats.js (before our patch) to avoid false positives
    # from our own guard text.
    chats_src = (
        clean_socket_dir
        / "node_modules"
        / "@whiskeysockets"
        / "baileys"
        / "lib"
        / "Socket"
        / "chats.js"
    ).read_text(encoding="utf-8")
    lines = chats_src.splitlines()
    # Find all lines that call updateProfileName outside its own definition
    auto_calls = []
    inside_update_profile_name = False
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("const updateProfileName = async"):
            inside_update_profile_name = True
            continue
        if inside_update_profile_name and stripped == "};":
            inside_update_profile_name = False
            continue
        if "updateProfileName(" in stripped and not inside_update_profile_name:
            auto_calls.append((i, stripped))
    assert auto_calls == [], (
        f"updateProfileName auto-fire callers found: {auto_calls}\n"
        "Hermes patch must block them or document reachability."
    )
