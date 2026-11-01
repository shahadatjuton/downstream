"""Static checks on the browser extension and the host registration scripts: one manifest that
loads in Chromium browsers and Firefox, and a gecko id that every registration path agrees on."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"


def test_manifest_targets_chromium_and_firefox():
    manifest = json.loads((EXT / "manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 3
    # Chromium runs the service worker; Firefox runs the same file as an event page.
    assert manifest["background"]["service_worker"] == "background.js"
    assert manifest["background"]["scripts"] == ["background.js"]
    gecko = manifest["browser_specific_settings"]["gecko"]
    assert re.fullmatch(r"[\w.-]+@[\w.-]+", gecko["id"])
    assert "nativeMessaging" in manifest["permissions"]


def test_gecko_id_is_consistent_everywhere():
    manifest = json.loads((EXT / "manifest.json").read_text("utf-8"))
    gecko_id = manifest["browser_specific_settings"]["gecko"]["id"]
    key = json.loads((EXT / ".extension-key.json").read_text("utf-8"))
    assert key["gecko_id"] == gecko_id
    iss = (ROOT / "packaging" / "installer.iss").read_text("utf-8")
    assert f'#define GeckoId "{gecko_id}"' in iss
    assert "Software\\Mozilla\\NativeMessagingHosts" in iss
    assert '"allowed_extensions": ["{#GeckoId}"]' in iss
    ps1 = (ROOT / "scripts" / "register-browser.ps1").read_text("utf-8")
    assert "Software\\Mozilla\\NativeMessagingHosts" in ps1
    assert "allowed_extensions" in ps1 and "$keyInfo.gecko_id" in ps1


def test_background_handles_missing_onDeterminingFilename():  # noqa: N802
    js = (EXT / "background.js").read_text("utf-8")
    assert "if (chrome.downloads.onDeterminingFilename)" in js
    assert "chrome.downloads.onCreated.addListener" in js
    assert js.count("interceptDownload(item)") >= 2
