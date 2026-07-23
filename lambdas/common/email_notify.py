"""
XOMTRACKS SES Notifications
===========================
Admin notification emails for the phone-link approval flow. On each new
POST /me/link-phone request, Dom (the admin) is emailed so he can approve or deny
it in the admin portal.

Sender identity + configuration set are provisioned by
xomtracks-infrastructure/terraform/ses.tf (domain identity xomtracks.xomware.com
with Easy DKIM, from noreply@xomtracks.xomware.com) and read lazily from SSM via
ssm_helpers. The Lambda role grants ses:SendEmail on that identity.

BEST-EFFORT: a send failure is logged and swallowed (returns False). The pending
request is the source of truth and is always visible in the admin portal, so a
transient SES error must never fail the request-creation call.
"""

import boto3

from lambdas.common.constants import ADMIN_EMAIL, AWS_DEFAULT_REGION, XOMTRACKS_ADMIN_URL
from lambdas.common.logger import get_logger

log = get_logger(__file__)

_SUBJECT = "Xomtracks: new phone-link request"


def _build_body(requester_email: str, phone: str, saved_name: str | None) -> str:
    name = saved_name if saved_name else "unknown"
    return (
        f"{requester_email} requested to link phone {phone} "
        f"(saved name: {name}). Approve or deny in the Xomtracks admin portal: "
        f"{XOMTRACKS_ADMIN_URL}"
    )


def send_link_request_notification(
    requester_email: str,
    phone: str,
    saved_name: str | None,
) -> bool:
    """
    Email the admin about a new phone-link request. Returns True on send,
    False if the send failed (logged, never raised).
    """
    # Imported lazily so the SSM lookup happens at call time (and stays easy to
    # pre-seed/mocked in tests), not at module import.
    from lambdas.common import ssm_helpers

    body = _build_body(requester_email, phone, saved_name)
    try:
        from_address = ssm_helpers.SES_FROM_ADDRESS
        configuration_set = ssm_helpers.SES_CONFIGURATION_SET
        client = boto3.client("sesv2", region_name=AWS_DEFAULT_REGION)
        client.send_email(
            FromEmailAddress=from_address,
            Destination={"ToAddresses": [ADMIN_EMAIL]},
            ConfigurationSetName=configuration_set,
            Content={
                "Simple": {
                    "Subject": {"Data": _SUBJECT},
                    "Body": {"Text": {"Data": body}},
                }
            },
        )
        log.info(f"Admin notified of link request from {requester_email} ({phone})")
        return True
    except Exception as err:
        # Best-effort: the pending request already exists and is visible in the
        # admin portal. Log and move on rather than failing the caller.
        log.error(f"Failed to send link-request notification email: {err}")
        return False
