"""
My Aerotermia - Alexa Smart Home skill for Panasonic Aquarea heat pumps.

One AWS Lambda function does two jobs:

1. Alexa Smart Home directives (trigger: "Alexa Smart Home")
   - Discovery: every linked user gets up to 3 virtual devices:
       <long_id>_cool  -> cooling on/off (zones ON in COOL / zones OFF)
       <long_id>_heat  -> heating on/off (zones ON in HEAT / zones OFF)
       <long_id>_dhw   -> domestic hot water tank on/off
     Turning cooling/heating OFF never touches the hot water tank, and
     turning the tank OFF never touches the zones.
   - Alexa.PowerController TurnOn / TurnOff
   - ReportState
   - Alexa.Authorization AcceptGrant

2. OAuth 2.0 account linking server (trigger: Lambda Function URL)
   GET  /authorize      -> Panasonic login page
   POST /authorize      -> log in to Panasonic (and ask for MFA code if needed)
   POST /authorize/mfa  -> finish MFA
   POST /token          -> authorization_code and refresh_token grants

Panasonic passwords are never stored: only the Panasonic refresh token,
encrypted with Fernet (key in the FERNET_KEY environment variable).
Our own OAuth codes/tokens are stored hashed (SHA-256) in DynamoDB.

Environment variables:
  TABLE_NAME           DynamoDB table (partition key "pk", string; TTL attribute "ttl")
  FERNET_KEY           32-byte urlsafe base64 key
  OAUTH_CLIENT_ID      must match the Client ID set in the Alexa console
  OAUTH_CLIENT_SECRET  must match the Client Secret set in the Alexa console
  NAME_COOL / NAME_HEAT / NAME_DHW   optional device names shown in Alexa
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
import urllib.parse
import uuid

import aiohttp
import boto3
from cryptography.fernet import Fernet, InvalidToken

from aioaquarea import (
    AgreementNotAcceptedError,
    AuthenticationError,
    Client,
    ExtendedOperationMode,
    MfaRequiredError,
    OperationStatus,
    UpdateOperationMode,
)
from aioaquarea.data import ZoneTemperatureSetUpdate

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
logging.getLogger("aioaquarea").setLevel(logging.WARNING)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
TABLE_NAME = os.environ.get("TABLE_NAME", "aquarea-alexa")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
NAMES = {
    "cool": os.environ.get("NAME_COOL", "Aerotermia Frío"),
    "heat": os.environ.get("NAME_HEAT", "Aerotermia Calor"),
    "dhw": os.environ.get("NAME_DHW", "Aerotermia Agua Caliente"),
}
DESCRIPTIONS = {
    "cool": "Heat pump cooling (zones on/off)",
    "heat": "Heat pump heating (zones on/off)",
    "dhw": "Heat pump domestic hot water tank",
}
CATEGORIES = {"cool": "AIR_CONDITIONER", "heat": "SWITCH", "dhw": "WATER_HEATER"}

ACCESS_TOKEN_TTL = 3600          # seconds
AUTH_CODE_TTL = 300              # seconds
MFA_PENDING_TTL = 600            # seconds
DEVICE_CACHE_TTL = 3600          # seconds
ALLOWED_REDIRECT_HOSTS = {"layla.amazon.com", "pitangui.amazon.com", "alexa.amazon.co.jp"}

COOL_MODES = (ExtendedOperationMode.COOL, ExtendedOperationMode.AUTO_COOL)
HEAT_MODES = (ExtendedOperationMode.HEAT, ExtendedOperationMode.AUTO_HEAT)
EXTENDED_TO_UPDATE = {
    ExtendedOperationMode.HEAT: UpdateOperationMode.HEAT,
    ExtendedOperationMode.COOL: UpdateOperationMode.COOL,
    ExtendedOperationMode.AUTO_HEAT: UpdateOperationMode.AUTO,
    ExtendedOperationMode.AUTO_COOL: UpdateOperationMode.AUTO,
}

# One event loop for the whole container, so aiohttp sessions (and pending MFA
# logins) survive between invocations of a warm Lambda.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

_table = None
_fernet = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["FERNET_KEY"].encode())
    return _fernet


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def now() -> int:
    return int(time.time())


class LinkError(Exception):
    """The user's Panasonic link is no longer valid."""


# Errors that mean "these credentials / this refresh token are no good".
# Anything else (API_ERROR, APP_VERSION_REJECTED...) is a temporary problem
# and must never unlink the user.
CREDENTIAL_ERRORS = {"TOKEN_EXPIRED", "1001-1401", "1000-1401", "1001-0001"}


def is_credential_error(err: Exception) -> bool:
    return str(getattr(err, "error_code", "")) in CREDENTIAL_ERRORS


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def user_id_for(email: str) -> str:
    return sha(email.strip().lower())[:32]


def save_user(uid: str, email: str, refresh_token: str) -> None:
    table().put_item(Item={
        "pk": f"user#{uid}",
        "email": email.strip().lower(),
        "rt": fernet().encrypt(refresh_token.encode()).decode(),
        "revoked": False,
        "updated": now(),
    })


def update_user_token(uid: str, refresh_token: str) -> None:
    table().update_item(
        Key={"pk": f"user#{uid}"},
        UpdateExpression="SET rt = :rt, updated = :u",
        ExpressionAttributeValues={
            ":rt": fernet().encrypt(refresh_token.encode()).decode(),
            ":u": now(),
        },
    )


def revoke_user(uid: str) -> None:
    table().update_item(
        Key={"pk": f"user#{uid}"},
        UpdateExpression="SET revoked = :r, updated = :u",
        ExpressionAttributeValues={":r": True, ":u": now()},
    )


def get_user(uid: str) -> dict | None:
    return table().get_item(Key={"pk": f"user#{uid}"}).get("Item")


def put_secret(kind: str, value: str, uid: str, ttl: int | None, **extra) -> None:
    item = {"pk": f"{kind}#{sha(value)}", "uid": uid, **extra}
    if ttl:
        item["ttl"] = now() + ttl
    table().put_item(Item=item)


def get_secret(kind: str, value: str) -> dict | None:
    if not value:
        return None
    return table().get_item(Key={"pk": f"{kind}#{sha(value)}"}).get("Item")


def delete_secret(kind: str, value: str) -> None:
    table().delete_item(Key={"pk": f"{kind}#{sha(value)}"})


def issue_tokens(uid: str, refresh_token: str | None = None) -> dict:
    access = secrets.token_urlsafe(32)
    put_secret("access", access, uid, ACCESS_TOKEN_TTL)
    if refresh_token is None:
        refresh_token = secrets.token_urlsafe(48)
        put_secret("refresh", refresh_token, uid, None)
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh_token,
    }


# --------------------------------------------------------------------------
# Panasonic sessions (cached per user in a warm container)
# --------------------------------------------------------------------------
_sessions: dict[str, dict] = {}      # uid -> {"session", "client", "devices", "devices_at"}
_pending_mfa: dict[str, dict] = {}   # nonce -> pending login


async def panasonic_for(uid: str) -> dict:
    cached = _sessions.get(uid)
    if cached:
        return cached

    user = get_user(uid)
    if not user or user.get("revoked"):
        raise LinkError("unknown or revoked user")
    try:
        rt = fernet().decrypt(user["rt"].encode()).decode()
    except InvalidToken as err:
        raise LinkError("cannot decrypt token") from err

    def on_new_token(token: str) -> None:
        try:
            update_user_token(uid, token)
        except Exception:  # never break a command because of storage
            LOGGER.exception("Could not store rotated Panasonic token")

    session = aiohttp.ClientSession()
    client = Client(session=session, refresh_token=rt,
                    refresh_token_callback=on_new_token, mfa_send_code=False)
    try:
        await client.login()
    except AuthenticationError as err:
        await session.close()
        if is_credential_error(err):
            revoke_user(uid)
            raise LinkError(str(err)) from err
        raise
    except BaseException:
        await session.close()
        raise
    entry = {"session": session, "client": client, "devices": None, "devices_at": 0}
    _sessions[uid] = entry
    return entry


async def drop_session(uid: str) -> None:
    entry = _sessions.pop(uid, None)
    if entry:
        await entry["session"].close()


async def device_infos(entry: dict, force: bool = False):
    if force or not entry["devices"] or now() - entry["devices_at"] > DEVICE_CACHE_TTL:
        entry["devices"] = await entry["client"].get_devices()
        entry["devices_at"] = now()
    return entry["devices"]


async def load_device(entry: dict, long_id: str):
    infos = await device_infos(entry)
    info = next((i for i in infos if i.long_id == long_id), None)
    if info is None:
        infos = await device_infos(entry, force=True)
        info = next((i for i in infos if i.long_id == long_id), None)
    if info is None:
        return None
    return await entry["client"].get_device(device_info=info)


# --------------------------------------------------------------------------
# Heat pump logic
# --------------------------------------------------------------------------
def any_zone_on(device) -> bool:
    return any(z.operation_status == OperationStatus.ON for z in device.zones.values())


def tank_on(device) -> bool:
    return bool(device.has_tank and device.tank
                and device.tank.operation_status == OperationStatus.ON)


def power_state(device, kind: str) -> str:
    if kind == "cool":
        return "ON" if any_zone_on(device) and device.mode in COOL_MODES else "OFF"
    if kind == "heat":
        return "ON" if any_zone_on(device) and device.mode in HEAT_MODES else "OFF"
    return "ON" if tank_on(device) else "OFF"


async def apply_state(client, device, *, zones_on: bool | None = None,
                      mode: UpdateOperationMode | None = None,
                      tank: bool | None = None) -> None:
    """Send one complete update, changing only what is asked.

    Mirrors the request aioaquarea's Device.set_mode sends (tested on a real
    unit): zone statuses, device operation status, tank status and the
    current zone setpoints.
    """
    zones = {
        zid: (OperationStatus.ON if zones_on else OperationStatus.OFF)
        if zones_on is not None else z.operation_status
        for zid, z in device.zones.items()
    }
    tank_status = (
        (OperationStatus.ON if tank else OperationStatus.OFF) if tank is not None
        else (OperationStatus.ON if tank_on(device) else OperationStatus.OFF)
    )
    if not device.has_tank:
        tank_status = OperationStatus.OFF

    something_on = tank_status == OperationStatus.ON or any(
        s == OperationStatus.ON for s in zones.values())
    operation_status = OperationStatus.ON if something_on else OperationStatus.OFF

    if not something_on:
        effective_mode = UpdateOperationMode.OFF
    elif mode is not None:
        effective_mode = mode
    else:
        effective_mode = EXTENDED_TO_UPDATE.get(device.mode, UpdateOperationMode.HEAT)

    setpoints = [
        ZoneTemperatureSetUpdate(zone_id=zid, heat_set=z.heat_target_temperature,
                                 cool_set=z.cool_target_temperature)
        for zid, z in device.zones.items() if z.supports_set_temperature
    ]
    await client.post_device_operation_update(
        device.long_id, effective_mode, zones, operation_status, tank_status, setpoints)


async def turn(client, device, kind: str, on: bool) -> None:
    if kind == "dhw":
        if not device.has_tank:
            raise ValueError("no tank")
        if tank_on(device) != on:
            await apply_state(client, device, tank=on)
        return

    wanted_mode = UpdateOperationMode.COOL if kind == "cool" else UpdateOperationMode.HEAT
    modes = COOL_MODES if kind == "cool" else HEAT_MODES
    if on:
        if not (any_zone_on(device) and device.mode in modes):
            await apply_state(client, device, zones_on=True, mode=wanted_mode)
    else:
        # Only switch zones off if they are running in THIS mode: turning
        # "cooling" off while heating is running must not stop the heating.
        if any_zone_on(device) and device.mode in modes:
            await apply_state(client, device, zones_on=False)


# --------------------------------------------------------------------------
# Alexa Smart Home
# --------------------------------------------------------------------------
def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.00Z")


def header(namespace: str, name: str, correlation: str | None = None) -> dict:
    h = {"namespace": namespace, "name": name, "payloadVersion": "3",
         "messageId": str(uuid.uuid4())}
    if correlation:
        h["correlationToken"] = correlation
    return h


def error_response(directive: dict, err_type: str, message: str) -> dict:
    d_header = directive.get("header", {})
    event = {"header": header("Alexa", "ErrorResponse", d_header.get("correlationToken")),
             "payload": {"type": err_type, "message": message}}
    endpoint = directive.get("endpoint")
    if endpoint:
        event["endpoint"] = {"endpointId": endpoint.get("endpointId")}
    return {"event": event}


def state_properties(state: str) -> list[dict]:
    t = iso_now()
    return [
        {"namespace": "Alexa.PowerController", "name": "powerState", "value": state,
         "timeOfSample": t, "uncertaintyInMilliseconds": 500},
        {"namespace": "Alexa.EndpointHealth", "name": "connectivity",
         "value": {"value": "OK"}, "timeOfSample": t, "uncertaintyInMilliseconds": 0},
    ]


def endpoint_object(long_id: str, kind: str, device_name: str) -> dict:
    return {
        "endpointId": f"{long_id}_{kind}",
        "manufacturerName": "My Aerotermia",
        "description": f"{DESCRIPTIONS[kind]} ({device_name})"[:128],
        "friendlyName": NAMES[kind],
        "displayCategories": [CATEGORIES[kind]],
        "cookie": {},
        "capabilities": [
            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
            {"type": "AlexaInterface", "interface": "Alexa.PowerController", "version": "3",
             "properties": {"supported": [{"name": "powerState"}],
                            "proactivelyReported": False, "retrievable": True}},
            {"type": "AlexaInterface", "interface": "Alexa.EndpointHealth", "version": "3",
             "properties": {"supported": [{"name": "connectivity"}],
                            "proactivelyReported": False, "retrievable": True}},
        ],
    }


def user_from_token(token: str) -> tuple[str | None, str | None]:
    """Return (uid, error_type)."""
    item = get_secret("access", token)
    if not item:
        return None, "INVALID_AUTHORIZATION_CREDENTIAL"
    if int(item.get("ttl", 0)) < now():
        return None, "EXPIRED_AUTHORIZATION_CREDENTIAL"
    return item["uid"], None


def split_endpoint(endpoint_id: str) -> tuple[str, str]:
    long_id, _, kind = endpoint_id.rpartition("_")
    if kind not in NAMES or not long_id:
        raise ValueError(endpoint_id)
    return long_id, kind


async def handle_directive(directive: dict) -> dict:
    h = directive["header"]
    namespace, name = h["namespace"], h["name"]

    if namespace == "Alexa.Authorization" and name == "AcceptGrant":
        return {"event": {"header": header("Alexa.Authorization", "AcceptGrant.Response"),
                          "payload": {}}}

    if namespace == "Alexa.Discovery":
        token = directive.get("payload", {}).get("scope", {}).get("token", "")
    else:
        token = directive.get("endpoint", {}).get("scope", {}).get("token", "")

    uid, err = user_from_token(token)
    if err:
        return error_response(directive, err, "Account link expired or invalid")

    try:
        entry = await panasonic_for(uid)
    except LinkError:
        return error_response(directive, "INVALID_AUTHORIZATION_CREDENTIAL",
                              "Panasonic account must be linked again")
    except AgreementNotAcceptedError:
        return error_response(directive, "INTERNAL_ERROR",
                              "Accept the new Panasonic terms in the Comfort Cloud app")
    except AuthenticationError as err:
        LOGGER.warning("Panasonic login problem (user kept linked): %s", err)
        return error_response(directive, "ENDPOINT_UNREACHABLE", "Panasonic login failed")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return error_response(directive, "ENDPOINT_UNREACHABLE", "Panasonic cloud unreachable")

    try:
        return await _handle_for_user(directive, namespace, name, uid, entry)
    except AuthenticationError:
        await drop_session(uid)
        return error_response(directive, "ENDPOINT_UNREACHABLE",
                              "Panasonic session expired, try again")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        await drop_session(uid)
        return error_response(directive, "ENDPOINT_UNREACHABLE", "Panasonic cloud unreachable")


async def _handle_for_user(directive, namespace, name, uid, entry) -> dict:
    client = entry["client"]

    if namespace == "Alexa.Discovery" and name == "Discover":
        endpoints = []
        for info in await device_infos(entry, force=True):
            cooling = any(getattr(z, "cool_mode", False) for z in info.zones)
            label = info.name or info.long_id
            if cooling:
                endpoints.append(endpoint_object(info.long_id, "cool", label))
            endpoints.append(endpoint_object(info.long_id, "heat", label))
            if info.has_tank:
                endpoints.append(endpoint_object(info.long_id, "dhw", label))
        return {"event": {"header": header("Alexa.Discovery", "Discover.Response"),
                          "payload": {"endpoints": endpoints}}}

    endpoint = directive["endpoint"]
    try:
        long_id, kind = split_endpoint(endpoint["endpointId"])
    except ValueError:
        return error_response(directive, "NO_SUCH_ENDPOINT", "Unknown endpoint")

    device = await load_device(entry, long_id)
    if device is None:
        return error_response(directive, "NO_SUCH_ENDPOINT", "Device not found")
    correlation = directive["header"].get("correlationToken")

    if namespace == "Alexa" and name == "ReportState":
        return {
            "event": {"header": header("Alexa", "StateReport", correlation),
                      "endpoint": {"endpointId": endpoint["endpointId"]}, "payload": {}},
            "context": {"properties": state_properties(power_state(device, kind))},
        }

    if namespace == "Alexa.PowerController" and name in ("TurnOn", "TurnOff"):
        on = name == "TurnOn"
        if kind == "cool" and on and not device.support_cooling:
            return error_response(directive, "NOT_SUPPORTED_IN_CURRENT_MODE",
                                  "This unit does not support cooling")
        await turn(client, device, kind, on)
        return {
            "event": {"header": header("Alexa", "Response", correlation),
                      "endpoint": {"endpointId": endpoint["endpointId"]}, "payload": {}},
            "context": {"properties": state_properties("ON" if on else "OFF")},
        }

    return error_response(directive, "INVALID_DIRECTIVE", f"Unsupported {namespace}.{name}")


# --------------------------------------------------------------------------
# OAuth account linking (Lambda Function URL)
# --------------------------------------------------------------------------
TEXTS = {
    "en": {
        "title": "Link your heat pump",
        "intro": "Sign in with the Panasonic ID you use in the Comfort Cloud app.",
        "email": "Email", "password": "Password", "submit": "Link account",
        "privacy": "Your password is only used now to sign in to Panasonic and is never stored.",
        "mfa_title": "Verification code",
        "mfa_intro": "Panasonic sent you a verification code{dest}. Enter it below.",
        "code": "Code", "mfa_submit": "Verify",
        "bad_login": "Panasonic rejected the email or password.",
        "bad_code": "Invalid or expired code. Try again.",
        "expired": "The sign-in took too long. Please start again from the Alexa app.",
        "no_devices": "This Panasonic account has no Aquarea heat pump.",
        "no_token": "Panasonic did not return a long-lived session. Please try again later.",
        "terms": "Panasonic has new terms. Accept them in the Comfort Cloud app and try again.",
        "unsupported_mfa": "Your account uses a verification method that is not supported "
                           "(use SMS or an authenticator app).",
        "error": "Something went wrong. Please try again.",
        "bad_request": "Invalid request. Start the linking from the Alexa app.",
    },
    "es": {
        "title": "Vincula tu aerotermia",
        "intro": "Inicia sesión con el Panasonic ID que usas en la app Comfort Cloud.",
        "email": "Email", "password": "Contraseña", "submit": "Vincular cuenta",
        "privacy": "Tu contraseña solo se usa ahora para entrar en Panasonic y nunca se guarda.",
        "mfa_title": "Código de verificación",
        "mfa_intro": "Panasonic te ha enviado un código de verificación{dest}. Escríbelo aquí.",
        "code": "Código", "mfa_submit": "Verificar",
        "bad_login": "Panasonic ha rechazado el email o la contraseña.",
        "bad_code": "Código incorrecto o caducado. Inténtalo de nuevo.",
        "expired": "El inicio de sesión ha tardado demasiado. Vuelve a empezar desde la app de Alexa.",
        "no_devices": "Esta cuenta de Panasonic no tiene ninguna aerotermia Aquarea.",
        "no_token": "Panasonic no ha devuelto una sesión duradera. Inténtalo más tarde.",
        "terms": "Panasonic tiene nuevas condiciones. Acéptalas en la app Comfort Cloud y vuelve a intentarlo.",
        "unsupported_mfa": "Tu cuenta usa un método de verificación no compatible "
                           "(usa SMS o una app de códigos).",
        "error": "Algo ha fallado. Inténtalo de nuevo.",
        "bad_request": "Petición no válida. Empieza la vinculación desde la app de Alexa.",
    },
}


def lang_of(headers: dict) -> str:
    accept = (headers.get("accept-language") or "").lower()
    return "es" if accept.startswith(("es", "ca", "gl", "eu")) or ",es" in accept else "en"


PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#f4f6f8;margin:0;
padding:24px 16px;color:#1d2731}}main{{max-width:420px;margin:0 auto;background:#fff;
border-radius:14px;padding:28px 24px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:22px;margin:0 0 8px}}p{{line-height:1.45}}label{{display:block;font-weight:600;
margin:16px 0 6px}}input{{width:100%;box-sizing:border-box;padding:12px;font-size:16px;
border:1px solid #c9d1d9;border-radius:8px}}button{{margin-top:22px;width:100%;padding:13px;
font-size:16px;font-weight:600;border:0;border-radius:8px;background:#0b6bcb;color:#fff;cursor:pointer}}
.err{{background:#fdecec;color:#a11;padding:10px 12px;border-radius:8px}}
.small{{font-size:13px;color:#5b6773}}</style></head><body><main>{body}</main></body></html>"""


def page(lang: str, body: str, status: int = 200) -> dict:
    t = TEXTS[lang]
    return {"statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "no-store", "X-Frame-Options": "DENY"},
            "body": PAGE.format(lang=lang, title=html.escape(t["title"]), body=body)}


def hidden(fields: dict) -> str:
    return "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                   for k, v in fields.items())


def login_page(lang: str, params: dict, error: str | None = None, email: str = "") -> dict:
    t = TEXTS[lang]
    err = f'<p class="err">{html.escape(t[error])}</p>' if error else ""
    body = (f"<h1>{t['title']}</h1><p>{t['intro']}</p>{err}"
            f'<form method="post" action="/authorize">{hidden(params)}'
            f'<label for="e">{t["email"]}</label>'
            f'<input id="e" name="email" type="email" autocomplete="username" required '
            f'value="{html.escape(email)}">'
            f'<label for="p">{t["password"]}</label>'
            f'<input id="p" name="password" type="password" autocomplete="current-password" required>'
            f'<button type="submit">{t["submit"]}</button></form>'
            f'<p class="small">{t["privacy"]}</p>')
    return page(lang, body, 400 if error == "bad_request" else 200)


def mfa_page(lang: str, nonce: str, destination: str | None, error: str | None = None) -> dict:
    t = TEXTS[lang]
    dest = f" ({html.escape(destination)})" if destination else ""
    err = f'<p class="err">{html.escape(t[error])}</p>' if error else ""
    body = (f"<h1>{t['mfa_title']}</h1><p>{t['mfa_intro'].format(dest=dest)}</p>{err}"
            f'<form method="post" action="/authorize/mfa">{hidden({"nonce": nonce})}'
            f'<label for="c">{t["code"]}</label>'
            f'<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" required>'
            f'<button type="submit">{t["mfa_submit"]}</button></form>')
    return page(lang, body)


def message_page(lang: str, key: str, status: int = 400) -> dict:
    return page(lang, f"<h1>{TEXTS[lang]['title']}</h1><p class='err'>{TEXTS[lang][key]}</p>", status)


def valid_redirect(uri: str) -> bool:
    try:
        p = urllib.parse.urlparse(uri)
    except ValueError:
        return False
    return (p.scheme == "https" and p.hostname in ALLOWED_REDIRECT_HOSTS
            and p.path.startswith("/api/skill/link/"))


def oauth_params(src: dict) -> dict | None:
    params = {k: src.get(k, "") for k in ("client_id", "redirect_uri", "state", "response_type")}
    if (not hmac.compare_digest(params["client_id"], OAUTH_CLIENT_ID)
            or params["response_type"] != "code" or not valid_redirect(params["redirect_uri"])):
        return None
    return params


def redirect_with_code(params: dict, uid: str) -> dict:
    code = secrets.token_urlsafe(32)
    put_secret("code", code, uid, AUTH_CODE_TTL, redirect_uri=params["redirect_uri"])
    query = urllib.parse.urlencode({"state": params["state"], "code": code})
    sep = "&" if "?" in params["redirect_uri"] else "?"
    return {"statusCode": 302, "headers": {"Location": f"{params['redirect_uri']}{sep}{query}",
                                           "Cache-Control": "no-store"}, "body": ""}


async def finish_link(lang: str, params: dict, email: str, client: Client) -> dict:
    devices = await client.get_devices()
    if not devices:
        return message_page(lang, "no_devices")
    token = client.refresh_token
    if not token:
        return message_page(lang, "no_token", 502)
    uid = user_id_for(email)
    save_user(uid, email, token)
    await drop_session(uid)          # forget any old cached session for this user
    return redirect_with_code(params, uid)


def _clean_pending() -> None:
    for nonce, p in list(_pending_mfa.items()):
        if now() - p["created"] > MFA_PENDING_TTL:
            _pending_mfa.pop(nonce, None)
            LOOP.create_task(p["session"].close())


async def http_authorize_post(lang: str, form: dict) -> dict:
    params = oauth_params(form)
    if not params:
        return message_page(lang, "bad_request")
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    if not email or not password:
        return login_page(lang, params, "bad_login", email)

    session = aiohttp.ClientSession()
    client = Client(session=session, username=email, password=password, mfa_send_code=True)
    keep_session = False
    try:
        await client.login()
        return await finish_link(lang, params, email, client)
    except MfaRequiredError as err:
        if err.challenge is None:
            return message_page(lang, "unsupported_mfa")
        _clean_pending()
        nonce = secrets.token_urlsafe(24)
        _pending_mfa[nonce] = {"client": client, "session": session, "email": email,
                               "params": params, "created": now()}
        keep_session = True
        return mfa_page(lang, nonce, err.challenge.destination)
    except AgreementNotAcceptedError:
        return message_page(lang, "terms")
    except AuthenticationError as err:
        if is_credential_error(err) or "MFA" in str(getattr(err, "error_code", "")):
            return login_page(lang, params, "bad_login", email)
        LOGGER.warning("Panasonic login failed: %s", err)
        return message_page(lang, "error", 502)
    except Exception:
        LOGGER.exception("Panasonic login failed")
        return message_page(lang, "error", 502)
    finally:
        if not keep_session:
            await session.close()


async def http_mfa_post(lang: str, form: dict) -> dict:
    nonce = form.get("nonce") or ""
    pending = _pending_mfa.get(nonce)
    if not pending or now() - pending["created"] > MFA_PENDING_TTL:
        return message_page(lang, "expired")
    try:
        await pending["client"].complete_mfa((form.get("code") or "").strip())
    except AuthenticationError as err:
        if str(getattr(err, "error_code", "")) == "MFA_INVALID_CODE":
            return mfa_page(lang, nonce, None, "bad_code")
        _pending_mfa.pop(nonce, None)
        await pending["session"].close()
        return message_page(lang, "expired")
    _pending_mfa.pop(nonce, None)
    try:
        return await finish_link(lang, pending["params"], pending["email"], pending["client"])
    except Exception:
        LOGGER.exception("Finishing link after MFA failed")
        return message_page(lang, "error", 502)
    finally:
        await pending["session"].close()


def json_response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store",
                        "Pragma": "no-cache"},
            "body": json.dumps(body)}


def client_authenticated(headers: dict, form: dict) -> bool:
    cid, secret = form.get("client_id", ""), form.get("client_secret", "")
    auth = headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            cid, _, secret = decoded.partition(":")
            cid, secret = urllib.parse.unquote(cid), urllib.parse.unquote(secret)
        except Exception:
            return False
    return (bool(OAUTH_CLIENT_SECRET)
            and hmac.compare_digest(cid, OAUTH_CLIENT_ID)
            and hmac.compare_digest(secret, OAUTH_CLIENT_SECRET))


def http_token(headers: dict, form: dict) -> dict:
    if not client_authenticated(headers, form):
        return json_response(401, {"error": "invalid_client"})
    grant = form.get("grant_type")

    if grant == "authorization_code":
        item = get_secret("code", form.get("code", ""))
        if not item or int(item.get("ttl", 0)) < now():
            return json_response(400, {"error": "invalid_grant"})
        delete_secret("code", form.get("code", ""))
        if form.get("redirect_uri") and form["redirect_uri"] != item.get("redirect_uri"):
            return json_response(400, {"error": "invalid_grant"})
        return json_response(200, issue_tokens(item["uid"]))

    if grant == "refresh_token":
        rt = form.get("refresh_token", "")
        item = get_secret("refresh", rt)
        if not item:
            return json_response(400, {"error": "invalid_grant"})
        user = get_user(item["uid"])
        if not user or user.get("revoked"):
            delete_secret("refresh", rt)
            return json_response(400, {"error": "invalid_grant"})
        return json_response(200, issue_tokens(item["uid"], refresh_token=rt))

    return json_response(400, {"error": "unsupported_grant_type"})


def parse_form(event: dict) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}


def handle_http(event: dict) -> dict:
    method = event["requestContext"]["http"]["method"].upper()
    path = (event.get("rawPath") or "/").rstrip("/") or "/"
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    lang = lang_of(headers)

    if path == "/authorize" and method == "GET":
        params = oauth_params(event.get("queryStringParameters") or {})
        if not params:
            return message_page(lang, "bad_request")
        return login_page(lang, params)
    if path == "/authorize" and method == "POST":
        return LOOP.run_until_complete(http_authorize_post(lang, parse_form(event)))
    if path == "/authorize/mfa" and method == "POST":
        return LOOP.run_until_complete(http_mfa_post(lang, parse_form(event)))
    if path == "/token" and method == "POST":
        return http_token(headers, parse_form(event))
    if path == "/" and method == "GET":
        return {"statusCode": 200, "headers": {"Content-Type": "text/plain"},
                "body": "My Aerotermia is running."}
    return {"statusCode": 404, "headers": {"Content-Type": "text/plain"}, "body": "Not found"}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def lambda_handler(event, context):
    if "directive" in event:
        directive = event["directive"]
        LOGGER.info("Directive %s.%s", directive["header"]["namespace"],
                    directive["header"]["name"])
        try:
            response = LOOP.run_until_complete(
                asyncio.wait_for(handle_directive(directive), timeout=7.5))
        except asyncio.TimeoutError:
            response = error_response(directive, "ENDPOINT_UNREACHABLE", "Panasonic too slow")
        except Exception:
            LOGGER.exception("Directive failed")
            response = error_response(directive, "INTERNAL_ERROR", "Unexpected error")
        if response["event"]["header"]["name"] == "ErrorResponse":
            LOGGER.warning("Error response: %s", response["event"]["payload"])
        return response

    if event.get("requestContext", {}).get("http"):
        try:
            return handle_http(event)
        except Exception:
            LOGGER.exception("HTTP request failed")
            return {"statusCode": 500, "headers": {"Content-Type": "text/plain"},
                    "body": "Internal error"}

    return {"statusCode": 400, "body": "Unsupported event"}
