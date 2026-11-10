"""Checks the YunoHost package files without a YunoHost server: manifest syntax, required
placeholders, and shell syntax. These do not prove the package installs; that needs a test server."""

import subprocess
import tomllib
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "package"


def test_manifest_parses_and_has_required_sections():
    manifest = tomllib.loads((PKG / "manifest.toml").read_text())
    assert manifest["packaging_format"] == 2
    assert manifest["id"].replace("-", "").isalnum() and manifest["id"] == manifest["id"].lower()
    assert manifest["integration"]["helpers_version"] == "2.1"
    res = manifest["resources"]
    for key in ("sources", "system_user", "install_dir", "data_dir", "permissions", "ports", "database", "apt"):
        assert key in res, f"missing resource {key}"
    assert res["permissions"]["main"]["url"] == "/"
    assert res["permissions"]["main"]["auth_header"] is True
    assert res["database"]["type"] == "postgresql"


def test_systemd_and_nginx_templates_use_known_placeholders():
    unit = (PKG / "conf" / "systemd.service").read_text()
    nginx = (PKG / "conf" / "nginx.conf").read_text()
    for placeholder in ("__APP__", "__INSTALL_DIR__", "__PORT__"):
        assert placeholder in unit
    for placeholder in ("__PATH__", "__PORT__"):
        assert placeholder in nginx


def test_scripts_are_valid_bash():
    for name in ("_common.sh", "install", "upgrade", "remove", "backup", "restore"):
        result = subprocess.run(["bash", "-n", str(PKG / "scripts" / name)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_restore_refuses_to_run_without_the_key():
    text = (PKG / "scripts" / "restore").read_text()
    assert text.index("key_file") < text.index("ynh_restore")
