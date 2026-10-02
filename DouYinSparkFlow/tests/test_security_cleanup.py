import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webui import app as app_module


REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_RE = re.compile(r"^secret: '([^']*)'$", re.MULTILINE)


def _find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # On Windows, System32\bash.exe is the WSL launcher and cannot see the
    # temporary Windows paths these tests use; prefer Git for Windows' bash.
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def _find_powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


def _clean_env(**extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PROXY_SUB_URL", "PROXY_CONTROLLER_SECRET", "APP_ROOT"}
    }
    env.update(extra)
    return env


class RemovedBrowserExtensionTests(unittest.TestCase):
    def test_extension_directory_is_gone(self):
        self.assertFalse(
            (REPO_ROOT / "DouYinSparkFlow" / "webui" / "static" / "multiPagePlugins").exists()
        )
        readme = (REPO_ROOT / "DouYinSparkFlow" / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("multiPagePlugins", readme)

    def test_extension_is_not_served_but_console_assets_are(self):
        client = TestClient(app_module.app)
        self.assertEqual(
            404, client.get("/static/multiPagePlugins/manifest.json").status_code
        )
        self.assertEqual(200, client.get("/static/app.css").status_code)
        self.assertEqual(200, client.get("/static/app.js").status_code)


class ProxyControllerSecretTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "proxy").mkdir()
        shutil.copy(REPO_ROOT / "proxy" / "config.example.yaml", self.tmp / "proxy")
        self.secret_file = self.tmp / "state" / "proxy" / "controller-secret"
        self.config_file = self.tmp / "proxy" / "config.yaml"

    def _config_text(self):
        return self.config_file.read_text(encoding="utf-8-sig")

    def _config_secret(self):
        matches = SECRET_RE.findall(self._config_text())
        self.assertEqual(1, len(matches), self._config_text())
        return matches[0]

    def _run_refresh(self, **extra_env):
        bash = _find_bash()
        if not bash:
            self.skipTest("bash is not available")
        shutil.copy(REPO_ROOT / "refresh_proxy.sh", self.tmp)
        return subprocess.run(
            [bash, "refresh_proxy.sh"],
            cwd=self.tmp,
            env=_clean_env(**extra_env),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_refresh_generates_and_reuses_the_secret(self):
        first = self._run_refresh()
        self.assertEqual(0, first.returncode, first.stderr)
        secret = self.secret_file.read_text(encoding="ascii").strip()
        self.assertRegex(secret, r"^[0-9a-f]{32}$")
        self.assertEqual(secret, self._config_secret())
        text = self._config_text()
        for line in (
            "mixed-port: 7890",
            "allow-lan: true",
            "bind-address: '*'",
            "external-controller: '0.0.0.0:9090'",
        ):
            self.assertIn(line, text)
        self.assertNotIn(secret, first.stdout + first.stderr)
        secret_mtime = self.secret_file.stat().st_mtime_ns

        # A subscription refresh replaces config.yaml wholesale.
        self.config_file.write_text("mode: rule\nrules:\n  - MATCH,DIRECT\n", encoding="utf-8")
        second = self._run_refresh()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(secret, self._config_secret())
        self.assertEqual(secret_mtime, self.secret_file.stat().st_mtime_ns)

    def test_refresh_prefers_the_configured_secret(self):
        self.assertEqual(0, self._run_refresh().returncode)
        generated = self.secret_file.read_text(encoding="ascii").strip()

        result = self._run_refresh(PROXY_CONTROLLER_SECRET="abc123")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("abc123", self._config_secret())
        self.assertEqual(generated, self.secret_file.read_text(encoding="ascii").strip())

    def test_refresh_rejects_a_secret_that_would_break_the_config(self):
        result = self._run_refresh(PROXY_CONTROLLER_SECRET="bad'value")
        self.assertNotEqual(0, result.returncode)
        # The secret is checked before the config is (re)written, so a rejected
        # value never leaves behind a config without one.
        self.assertFalse(self.config_file.exists())

    def test_refresh_reads_an_empty_secret_from_a_crlf_env_file(self):
        (self.tmp / ".env").write_bytes(b"PROXY_SUB_URL=\r\nPROXY_CONTROLLER_SECRET=\r\n")
        result = self._run_refresh()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(self._config_secret(), r"^[0-9a-f]{32}$")

    def test_local_windows_installer_writes_the_same_secret(self):
        powershell = _find_powershell()
        if not powershell:
            self.skipTest("PowerShell is not available")
        script = REPO_ROOT / "deploy" / "install-local.ps1"
        # Load only the function definitions so the test never touches Docker.
        command = (
            "$ErrorActionPreference = 'Stop'; "
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$null); "
            "$defs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false); "
            "$text = ($defs | ForEach-Object { $_.Extent.Text }) -join [Environment]::NewLine; "
            ". ([scriptblock]::Create($text)); "
            f"Set-Location '{self.tmp}'; "
            "Initialize-ProxyConfig"
        )

        def run():
            return subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                env=_clean_env(),
                capture_output=True,
                text=True,
                timeout=120,
            )

        first = run()
        self.assertEqual(0, first.returncode, first.stderr)
        secret = self.secret_file.read_text(encoding="ascii").strip()
        self.assertRegex(secret, r"^[0-9a-f]{32}$")
        self.assertEqual(secret, self._config_secret())
        self.assertIn("external-controller: '0.0.0.0:9090'", self._config_text())
        self.assertNotIn(secret, first.stdout + first.stderr)

        self.config_file.write_text("mode: rule\n", encoding="utf-8")
        second = run()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(secret, self._config_secret())


if __name__ == "__main__":
    unittest.main()
