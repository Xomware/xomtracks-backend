"""
RED-before-GREEN: SES notification helper (lambdas/common/email_notify.py).

On each new phone-link request, Dom (the admin) is emailed via SES so he can
approve/deny it in the admin portal. This helper builds + sends that email. It
is BEST-EFFORT: a send failure must be logged and swallowed (returns False) so
the pending request is still created and visible in the admin portal.
"""

from unittest.mock import MagicMock

import pytest


class TestSendLinkRequestNotification:
    def test_sends_email_with_expected_content(self, monkeypatch):
        from lambdas.common import email_notify

        fake_client = MagicMock()
        monkeypatch.setattr(email_notify.boto3, "client", lambda *a, **k: fake_client)

        ok = email_notify.send_link_request_notification(
            requester_email="member@example.com",
            phone="3364042196",
            saved_name="Big Al",
        )
        assert ok is True

        fake_client.send_email.assert_called_once()
        kwargs = fake_client.send_email.call_args.kwargs
        assert kwargs["FromEmailAddress"] == "noreply@xomtracks.xomware.com"
        assert kwargs["Destination"]["ToAddresses"] == ["dominickj.giordano@gmail.com"]
        assert kwargs["ConfigurationSetName"] == "xomtracks-notifications"

        body = kwargs["Content"]["Simple"]["Body"]["Text"]["Data"]
        assert "member@example.com" in body
        assert "3364042196" in body
        assert "Big Al" in body

    def test_unknown_saved_name_renders_unknown(self, monkeypatch):
        from lambdas.common import email_notify

        fake_client = MagicMock()
        monkeypatch.setattr(email_notify.boto3, "client", lambda *a, **k: fake_client)

        email_notify.send_link_request_notification(
            requester_email="new@example.com", phone="2025550000", saved_name=None
        )
        body = fake_client.send_email.call_args.kwargs["Content"]["Simple"]["Body"]["Text"]["Data"]
        assert "unknown" in body.lower()

    def test_send_failure_is_swallowed_and_returns_false(self, monkeypatch):
        from lambdas.common import email_notify

        fake_client = MagicMock()
        fake_client.send_email.side_effect = RuntimeError("SES down")
        monkeypatch.setattr(email_notify.boto3, "client", lambda *a, **k: fake_client)

        ok = email_notify.send_link_request_notification(
            requester_email="member@example.com", phone="3364042196", saved_name=None
        )
        assert ok is False
