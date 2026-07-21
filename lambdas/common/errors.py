"""
XOMTRACKS Error Classes
=======================
Standardized error handling for all Lambda functions. Ported from
xomify-backend's lambdas/common/errors.py; trimmed/extended to the error
types xomtracks actually needs.
"""

import json
import traceback
from typing import Optional
from lambdas.common.logger import get_logger

log = get_logger(__file__)


class XomtracksError(Exception):
    """
    Base exception class for all Xomtracks errors.

    Usage:
        raise XomtracksError("Something went wrong", status=400)

    Or catch and convert to response:
        except XomtracksError as e:
            return e.to_response()
    """

    def __init__(
        self,
        message: str,
        handler: str = "unknown",
        function: str = "unknown",
        status: int = 500,
        details: Optional[dict] = None,
    ):
        # Guard against empty messages -- str(err) for some boto3 / generic
        # exceptions returns "", which produces an unhelpful
        # {"error": {"message": ""}} response.
        self.message = message if (isinstance(message, str) and message.strip()) else "unknown error"
        self.handler = handler
        self.function = function
        self.status = status
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Convert error to dictionary for JSON response."""
        return {
            "error": {
                "message": self.message,
                "handler": self.handler,
                "function": self.function,
                "status": self.status,
                **self.details,
            }
        }

    def to_response(self, is_api: bool = True) -> dict:
        """Convert error to Lambda response format."""
        body = self.to_dict()
        return {
            "statusCode": self.status,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Content-Type": "application/json",
            },
            "body": json.dumps(body) if is_api else body,
            "isBase64Encoded": False,
        }

    def log_error(self):
        """Log the error with full context."""
        log.error(f"XomtracksError in {self.handler}.{self.function}: {self.message}")
        if self.details:
            log.error(f"   Details: {self.details}")

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


# ============================================
# Specific Error Types
# ============================================

class AuthorizationError(XomtracksError):
    """Raised when authorization fails (missing/invalid JWT or bearer key)."""

    def __init__(self, message: str = "Unauthorized", handler: str = "authorizer", function: str = "unknown"):
        super().__init__(message=message, handler=handler, function=function, status=401)


class MissingCallerIdentityError(AuthorizationError):
    """
    Raised when caller identity (email) cannot be resolved from the
    authorizer context populated by the custom Lambda authorizer.
    """

    def __init__(self, field: str = "email", handler: str = "utility_helpers", function: str = "unknown"):
        message = f"Missing caller identity: '{field}' not present in authorizer context"
        super().__init__(message=message, handler=handler, function=function)
        self.details = {"field": field}


class ValidationError(XomtracksError):
    """Raised when input validation fails (bad request shape/values)."""

    def __init__(self, message: str, handler: str = "unknown", function: str = "unknown", field: str = None):
        details = {"field": field} if field else {}
        super().__init__(message=message, handler=handler, function=function, status=400, details=details)


class ForbiddenError(XomtracksError):
    """Raised when the caller is identified but not permitted to perform the action."""

    def __init__(self, message: str = "Forbidden", handler: str = "unknown", function: str = "unknown", reason: str = None):
        details = {"reason": reason} if reason else {}
        super().__init__(message=message, handler=handler, function=function, status=403, details=details)


class NotFoundError(XomtracksError):
    """Raised when a resource is not found."""

    def __init__(self, message: str, handler: str = "unknown", function: str = "unknown", resource: str = None):
        details = {"resource": resource} if resource else {}
        super().__init__(message=message, handler=handler, function=function, status=404, details=details)


class DynamoDBError(XomtracksError):
    """Raised when DynamoDB operations fail."""

    def __init__(self, message: str, handler: str = "dynamo_helpers", function: str = "unknown", table: str = None):
        details = {"table": table} if table else {}
        super().__init__(message=message, handler=handler, function=function, status=500, details=details)


class SpotifyAPIError(XomtracksError):
    """Raised when a Spotify API call fails (transport or non-2xx response)."""

    def __init__(self, message: str, handler: str = "spotify", function: str = "unknown", endpoint: str = None):
        details = {"endpoint": endpoint} if endpoint else {}
        super().__init__(message=message, handler=handler, function=function, status=502, details=details)


class MatchingError(XomtracksError):
    """Raised when cross-platform resolution/matching fails unexpectedly."""

    def __init__(self, message: str, handler: str = "matching", function: str = "unknown", platform: str = None):
        details = {"platform": platform} if platform else {}
        super().__init__(message=message, handler=handler, function=function, status=502, details=details)


# ============================================
# Sensitive Data Masking
# ============================================

SENSITIVE_FIELDS = {
    'accessToken', 'access_token',
    'refreshToken', 'refresh_token',
    'password', 'passwd',
    'secret', 'apiKey', 'api_key',
    'authorization', 'Authorization',
    'x-api-key', 'X-API-Key',
    'sessionToken', 'session_token',
    'bearerKey', 'bearer_key',
}


def mask_sensitive_data(data, mask_value="***MASKED***"):
    """Recursively mask sensitive fields in dictionaries and lists."""
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            if key in SENSITIVE_FIELDS or any(
                sensitive.lower() in key.lower() for sensitive in ['token', 'password', 'secret', 'key', 'auth']
            ):
                masked[key] = mask_value
            else:
                masked[key] = mask_sensitive_data(value, mask_value)
        return masked
    elif isinstance(data, list):
        return [mask_sensitive_data(item, mask_value) for item in data]
    elif isinstance(data, str) and len(data) > 100:
        return data[:50] + "...[truncated]..." + data[-20:]
    else:
        return data


def log_error_context(handler_name: str, function_name: str, event: dict, context=None):
    """Log relevant context information when an error occurs."""
    try:
        http_method = event.get('httpMethod', event.get('requestContext', {}).get('http', {}).get('method', 'N/A'))
        path = event.get('path', event.get('rawPath', 'N/A'))
        query_params = mask_sensitive_data(event.get('queryStringParameters', {}))
        headers = mask_sensitive_data(event.get('headers', {}))

        log.error(f"Error Context for {handler_name}.{function_name}:")
        log.error(f"   Method: {http_method}")
        log.error(f"   Path: {path}")
        log.error(f"   Query Params: {query_params}")
        log.error(f"   Headers: {headers}")

        if event.get('body'):
            try:
                body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
                safe_body = mask_sensitive_data(body)
                log.error(f"   Body: {safe_body}")
            except Exception:
                log.error("   Body: [Unable to parse]")

        if context:
            log.error(f"   Request ID: {getattr(context, 'aws_request_id', 'N/A')}")
            log.error(f"   Function: {getattr(context, 'function_name', 'N/A')}")

    except Exception as log_error:
        log.error(f"Error while logging context: {log_error}")


# ============================================
# Error Handler Decorator
# ============================================

def handle_errors(handler_name: str, log_context: bool = True):
    """
    Decorator to handle errors consistently across handlers.

    Usage:
        @handle_errors("shares_ingest")
        def handler(event, context):
            ...
    """

    def decorator(func):
        def wrapper(event, context):
            try:
                return func(event, context)
            except XomtracksError as e:
                e.log_error()
                if log_context:
                    log_error_context(handler_name, func.__name__, event, context)
                return e.to_response()
            except Exception as e:
                log.error(f"Unexpected error in {handler_name}: {str(e)}")
                log.error(traceback.format_exc())

                if log_context:
                    log_error_context(handler_name, func.__name__, event, context)

                raw_message = str(e) or repr(e) or e.__class__.__name__
                error = XomtracksError(
                    message=raw_message,
                    handler=handler_name,
                    function=func.__name__,
                    status=500,
                )
                return error.to_response()

        return wrapper

    return decorator
