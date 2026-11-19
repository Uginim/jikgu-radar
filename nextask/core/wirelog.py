"""Lossless JSON/text transport records, except credentials, server addresses and image bodies."""
import base64
import binascii
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit

from .config import SECRET_NAMES

MASK = "[가림]"
_PRIVATE = set()
_SECRET_KEYS = {"authorization", "proxyauthorization", "xapikey", "apikey", "authtoken", "accesstoken", "refreshtoken", "cookie", "setcookie"}
_IMAGE_KEYS = {"image", "images", "imageb64", "imagebase64", "imagedata"}


@contextmanager
def case_scope(case_id):
    """Restore attribution when FastAPI reuses the same worker thread for another case."""
    from . import caselog
    previous = getattr(caselog.CURRENT, "case_id", None)
    caselog.CURRENT.case_id = case_id
    try:
        yield
    finally:
        caselog.CURRENT.case_id = previous


def _key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _text(value):
    secrets = [os.environ.get(name, "") for name in SECRET_NAMES]
    addresses = (os.environ.get("ZORO_URLS") or os.environ.get("ZORO_URL") or "").split(",")
    for secret in sorted({*_PRIVATE, *secrets, *addresses}, key=len, reverse=True):
        if secret and len(secret) >= 4:
            value = value.replace(secret, MASK).replace(quote(secret, safe=""), MASK)
    return value


def image_metadata(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "payload": "image omitted; hash retained"}


def file_image(path):
    if not path:
        return None
    try:
        with open(path, "rb") as stream:
            return {"path": str(path), **image_metadata(stream.read())}
    except OSError as error:
        return {"path": str(path), "metadata_error": f"{type(error).__name__}: {error}"}


def sanitize(value, key=""):
    name = _key(key)
    if name in _SECRET_KEYS or name in {"serverurl", "endpoint", "remoteurl"}:
        return MASK if value else value
    if isinstance(value, dict):
        return {k: sanitize(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, key) for v in value]
    if isinstance(value, str):
        if value.startswith("data:image/") or name in _IMAGE_KEYS and len(value) > 64:
            payload = value.partition(",")[2] if value.startswith("data:image/") else value
            try:
                return image_metadata(base64.b64decode(re.sub(r"\s+", "", payload), validate=True))
            except (ValueError, binascii.Error):
                pass
        return _text(value)
    if isinstance(value, bytes):
        return wire_body(value)
    return value


def wire_body(raw):
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        safe = raw
        for secret in {*_PRIVATE, *(os.environ.get(name, "") for name in SECRET_NAMES)}:
            if secret:
                safe = safe.replace(secret.encode(), MASK.encode())
        return {"encoding": "base64", "data": base64.b64encode(safe).decode()}
    try:
        parsed = json.loads(text)
    except ValueError:
        return _text(text)
    safe = sanitize(parsed)
    return text if safe == parsed else json.dumps(safe, ensure_ascii=False)


def logged_json_request(request, timeout, *, opener, request_id=None):
    """Send the given urllib Request unchanged; record every attempt, including malformed/error responses."""
    from . import caselog
    address = urlsplit(request.full_url)
    _PRIVATE.update((f"{address.scheme}://{address.netloc}", address.netloc))
    if address.hostname:
        _PRIVATE.add(address.hostname)
    for name, value in request.header_items():
        if _key(name) in _SECRET_KEYS and value:
            _PRIVATE.update((value, value.rsplit(" ", 1)[-1]))
    started = time.perf_counter()
    raw, status, headers, error = None, None, {}, None
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            headers = dict(getattr(response, "headers", {}) or {})
            raw = response.read()
        return json.loads(raw)
    except HTTPError as exc:
        status, headers = exc.code, dict(exc.headers or {})
        raw = exc.read()
        setattr(exc, "raw_body", raw)
        error = f"{type(exc).__name__}: {exc}"
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        partial = getattr(exc, "partial", None)
        if raw is None and isinstance(partial, bytes):
            raw = partial
        raise
    finally:
        sent = request.data or b""
        cid = getattr(caselog.CURRENT, "case_id", None) or "transport-unassigned"
        caselog.log_event(cid, "http_attempt", transport_id=uuid.uuid4().hex, request_id=request_id,
                          raw_request={"method": request.get_method(), "server_url": MASK,
                                       "path": address.path + ("?" + address.query if address.query else ""),
                                       "headers": sanitize(dict(request.header_items())), "body": wire_body(sent),
                                       "bytes": len(sent), "sha256": hashlib.sha256(sent).hexdigest()},
                          raw_response=None if status is None and raw is None else {
                              "status": status, "headers": sanitize(headers), "body": wire_body(raw),
                              "bytes": len(raw) if raw is not None else None,
                              "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None},
                          error=error, latency_ms=round((time.perf_counter() - started) * 1000, 1))
