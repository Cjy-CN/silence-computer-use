"""Offline regressions: no live credentials, desktop input or session creation."""

import ctypes
import os
import unittest
from unittest.mock import Mock, patch

from vdcu.authorization import WindowsCredentialApi, authorize_session, probe_empty_credential
from vdcu.shadow_engine import PipSdk


class CredentialTests(unittest.TestCase):
    def api(self, result=(True, 0), domain="PC"):
        api = Mock()
        api.identity.return_value = (domain, "test-user", "pc")
        api.try_empty.return_value = result
        return api

    def test_local_empty_accepted(self):
        api = self.api()
        result = probe_empty_credential(api)
        self.assertTrue(result.accepted)
        self.assertTrue(result.try_without_card)
        api.try_empty.assert_called_once_with("PC", "test-user")

    def test_nonempty_password_prompts(self):
        api = self.api((False, 1326))
        result = probe_empty_credential(api)
        self.assertFalse(result.accepted)
        self.assertFalse(result.try_without_card)
        self.assertEqual(result.status, "password_required")
        api.try_empty.assert_called_once()

    def test_account_restriction_is_not_authentication_success(self):
        result = probe_empty_credential(self.api((False, 1327)))
        self.assertFalse(result.accepted)
        self.assertTrue(result.try_without_card)
        self.assertEqual(result.status, "account_restriction")

    def test_unknown_failure_does_not_claim_empty(self):
        result = probe_empty_credential(self.api((False, 5)))
        self.assertFalse(result.accepted)
        self.assertFalse(result.try_without_card)

    def test_nonlocal_account_is_not_probed(self):
        api = self.api(domain="DOMAIN")
        self.assertFalse(probe_empty_credential(api).try_without_card)
        api.try_empty.assert_not_called()

    def test_identity_failure_is_inconclusive(self):
        api = self.api()
        api.identity.side_effect = OSError("identity unavailable")
        self.assertFalse(probe_empty_credential(api).try_without_card)
        api.try_empty.assert_not_called()

    def test_probe_token_is_closed_not_used(self):
        api = WindowsCredentialApi.__new__(WindowsCredentialApi)
        api.advapi = Mock()
        api.kernel = Mock()

        def logon(account, domain, password, kind, provider, token):
            self.assertEqual((password, kind, provider), ("", 2, 0))
            token._obj.value = 123
            return 1

        api.advapi.LogonUserW.side_effect = logon
        self.assertEqual(api.try_empty("PC", "test-user"), (True, 0))
        api.kernel.CloseHandle.assert_called_once()
        self.assertEqual(api.kernel.CloseHandle.call_args.args[0].value, 123)


class SessionTests(unittest.TestCase):
    def fixtures(self):
        host, ui = Mock(), Mock()
        host.create_session.return_value = {"session_guid": "test-session"}
        host.wait_for_render.return_value = {"rendered": True}
        ui.dialog.cancelled = False
        ui.dialog.submitted = True
        ui.dialog.read_password.return_value = "test-only-secret"
        return host, ui

    def test_automatic_empty_never_reads_or_shows_card(self):
        host, ui = self.fixtures()
        result = authorize_session(host, ui, auto_empty=True, timeout=1)
        self.assertTrue(result["ok"])
        host.create_session.assert_called_once_with("")
        ui.dialog.read_password.assert_not_called()
        ui.show_dialog.assert_not_called()
        ui.close_dialog.assert_not_called()

    def test_manual_password_is_forwarded_once(self):
        host, ui = self.fixtures()
        result = authorize_session(host, ui, auto_empty=False, timeout=1)
        self.assertTrue(result["ok"])
        host.create_session.assert_called_once_with("test-only-secret")
        ui.close_dialog.assert_called_once()
        self.assertNotIn("test-only-secret", repr(result))

    def test_manual_empty_submission_is_allowed(self):
        host, ui = self.fixtures()
        ui.dialog.read_password.return_value = ""
        self.assertTrue(authorize_session(host, ui, auto_empty=False, timeout=1)["ok"])
        host.create_session.assert_called_once_with("")

    def test_guid_without_render_is_failure_no_retry(self):
        host, ui = self.fixtures()
        host.wait_for_render.return_value = {"rendered": False}
        result = authorize_session(host, ui, auto_empty=True, timeout=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "session_render_failed")
        host.create_session.assert_called_once()
        ui.show_dialog.assert_not_called()

    def test_creation_failure_does_not_render_or_prompt_again(self):
        host, ui = self.fixtures()
        host.create_session.return_value = {}
        self.assertFalse(authorize_session(host, ui, auto_empty=True, timeout=1)["ok"])
        host.wait_for_render.assert_not_called()
        ui.show_dialog.assert_not_called()

    def test_manual_failure_does_not_loop(self):
        host, ui = self.fixtures()
        host.wait_for_render.return_value = {"rendered": False}
        self.assertFalse(authorize_session(host, ui, auto_empty=False, timeout=1)["ok"])
        host.create_session.assert_called_once()
        ui.show_dialog.assert_not_called()

    def test_cancellation_stops_without_sdk_attempt(self):
        host, ui = self.fixtures()
        ui.dialog.cancelled = True
        result = authorize_session(host, ui, auto_empty=False, timeout=1)
        self.assertEqual(result["exit_code"], 4)
        host.create_session.assert_not_called()

    def test_timeout_stops_without_sdk_attempt(self):
        host, ui = self.fixtures()
        ui.dialog.submitted = False
        now = Mock(side_effect=[0, 0.1, 2])
        result = authorize_session(host, ui, auto_empty=False, timeout=1, now=now, sleep=Mock())
        self.assertEqual(result["reason"], "authorization_timeout")
        host.create_session.assert_not_called()


class BindingTests(unittest.TestCase):
    def test_empty_password_is_not_a_null_pointer(self):
        sdk = PipSdk.__new__(PipSdk)
        sdk.dll = Mock()
        sdk._tail = lambda: []
        for secret, encoded in (("", b""), (None, None), ("test-only", b"test-only")):
            with self.subTest(secret=secret):
                sdk.create_shadow_session(1, password=secret, prompt=0)
                self.assertEqual(sdk.dll.PipSdk_CreateShadowSession.call_args.args[8], encoded)


@unittest.skipUnless(os.name == "nt", "native window bindings require Windows")
class UiTests(unittest.TestCase):
    def test_hidden_render_host_does_not_create_password_card(self):
        from vdcu import shadow_host
        with patch.object(shadow_host, "PasswordDialog") as dialog, \
                patch.object(shadow_host, "_register_and_window", return_value=1), \
                patch.object(shadow_host.ole32, "CoInitializeEx"), \
                patch.object(shadow_host.user32, "GetSystemMetrics", return_value=1920), \
                patch.object(shadow_host.user32, "SetWindowRgn"), \
                patch.object(shadow_host.gdi32, "CreateSolidBrush", return_value=1), \
                patch.object(shadow_host.gdi32, "CreateRoundRectRgn", return_value=1):
            dialog.return_value.scale = 1
            ui = shadow_host.UiThread(show_authorization=False)
            ui.stop_pumping.set()
            ui.run()
            self.assertTrue(ui.ready.is_set())
            dialog.return_value.create.assert_not_called()

    def test_password_account_creates_card(self):
        from vdcu import shadow_host
        with patch.object(shadow_host, "PasswordDialog") as dialog, \
                patch.object(shadow_host, "_register_and_window", return_value=1), \
                patch.object(shadow_host.ole32, "CoInitializeEx"), \
                patch.object(shadow_host.user32, "GetSystemMetrics", return_value=1920), \
                patch.object(shadow_host.user32, "SetWindowRgn"), \
                patch.object(shadow_host.gdi32, "CreateSolidBrush", return_value=1), \
                patch.object(shadow_host.gdi32, "CreateRoundRectRgn", return_value=1):
            dialog.return_value.scale = 1
            ui = shadow_host.UiThread(show_authorization=True)
            ui.stop_pumping.set()
            ui.run()
            dialog.return_value.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
