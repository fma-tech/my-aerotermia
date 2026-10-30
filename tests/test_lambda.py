"""Offline tests for my-aerotermia/lambda_function.py with fake Panasonic + DynamoDB."""
import base64, json, os, re, sys, time, urllib.parse
from cryptography.fernet import Fernet

os.environ.update(FERNET_KEY=Fernet.generate_key().decode(), OAUTH_CLIENT_ID="alexa-skill",
                  OAUTH_CLIENT_SECRET="s3cr3t", TABLE_NAME="t", AWS_DEFAULT_REGION="eu-west-1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lambda_function as L
from aioaquarea import (AuthenticationError, ExtendedOperationMode as X, MfaRequiredError,
                        OperationStatus as O, UpdateOperationMode as U)
from aioaquarea.mfa import MfaChallenge
from aioaquarea.errors import AppVersionRejectedError

# ---------------- fake DynamoDB ----------------
class Table:
    def __init__(s): s.d = {}
    def put_item(s, Item): s.d[Item["pk"]] = dict(Item)
    def get_item(s, Key):
        i = s.d.get(Key["pk"]); return {"Item": dict(i)} if i else {}
    def delete_item(s, Key): s.d.pop(Key["pk"], None)
    def update_item(s, Key, UpdateExpression, ExpressionAttributeValues):
        it = s.d.setdefault(Key["pk"], {"pk": Key["pk"]})
        for part in UpdateExpression[4:].split(","):
            k, v = [x.strip() for x in part.split("=")]
            it[k] = ExpressionAttributeValues[v]
TABLE = Table(); L._table = TABLE

# ---------------- fake Panasonic ----------------
class E: pass
class Zone:
    def __init__(s): s.operation_status = O.OFF; s.heat_target_temperature = 0
    cool_target_temperature = 20; supports_set_temperature = True
class Tank:
    def __init__(s): s.operation_status = O.ON
class Device:
    def __init__(s):
        s.long_id = "B944201905"; s.zones = {1: Zone()}; s.mode = X.COOL; s.has_tank = True
        s.tank = Tank(); s.support_cooling = True; s.op = O.ON; s.posts = []
DEV = Device()
class ZInfo: cool_mode = True
class Info: long_id = "B944201905"; name = "aerotermia"; has_tank = True; zones = [ZInfo()]
STATE = {"mfa": False, "refresh_ok": True, "version_rejected": False, "logins": 0}
UPD = {U.COOL: X.COOL, U.HEAT: X.HEAT, U.OFF: X.OFF, U.AUTO: X.AUTO_HEAT}

class FakeClient:
    def __init__(s, session=None, username=None, password=None, refresh_token=None,
                 refresh_token_callback=None, mfa_send_code=True):
        s.u, s.p, s.rt, s.cb = username, password, refresh_token, refresh_token_callback
        s.refresh_token = None
    async def login(s):
        STATE["logins"] += 1
        if STATE["version_rejected"]:
            raise AppVersionRejectedError()
        if s.p is None:
            if not STATE["refresh_ok"]:
                raise AuthenticationError("TOKEN_EXPIRED", "The refresh token was rejected")
            s.refresh_token = "PANA-RT-2"; s.cb("PANA-RT-2"); return
        if s.p != "good":
            raise AuthenticationError("1001-1401", "bad")
        if STATE["mfa"]:
            raise MfaRequiredError(MfaChallenge("sms", "+34*****12"))
        s.refresh_token = "PANA-RT-1"
    async def complete_mfa(s, code):
        if code != "123456":
            raise AuthenticationError("MFA_INVALID_CODE", "wrong")
        s.refresh_token = "PANA-RT-MFA"
    async def get_devices(s): return [Info()]
    async def get_device(s, device_info): return DEV
    async def post_device_operation_update(s, long_id, mode, zones, op, tank, setpoints):
        DEV.posts.append((mode, dict(zones), op, tank))
        for zid, st in zones.items(): DEV.zones[zid].operation_status = st
        DEV.tank.operation_status = tank; DEV.op = op; DEV.mode = UPD[mode]
L.Client = FakeClient

# ---------------- helpers ----------------
REDIRECT = "https://layla.amazon.com/api/skill/link/M2ABCDEF"
def http(method, path, query=None, form=None, headers=None, b64=False):
    body = urllib.parse.urlencode(form) if form else ""
    if b64: body = base64.b64encode(body.encode()).decode()
    ev = {"rawPath": path, "requestContext": {"http": {"method": method}},
          "headers": headers or {"accept-language": "es-ES,es;q=0.9"},
          "queryStringParameters": query, "body": body, "isBase64Encoded": b64}
    return L.lambda_handler(ev, None)
def basic(cid="alexa-skill", sec="s3cr3t"):
    return {"authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()}
OP = {"client_id": "alexa-skill", "redirect_uri": REDIRECT, "state": "st8", "response_type": "code"}
def directive(ns, name, token, endpoint=None):
    d = {"header": {"namespace": ns, "name": name, "payloadVersion": "3", "messageId": "m",
                    "correlationToken": "corr"}, "payload": {}}
    if endpoint: d["endpoint"] = {"endpointId": endpoint, "scope": {"type": "BearerToken", "token": token}}
    else: d["payload"] = {"scope": {"type": "BearerToken", "token": token}}
    return L.lambda_handler({"directive": d}, None)
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: ok.fails += 1
ok.fails = 0

# ---------------- OAuth ----------------
r = http("GET", "/authorize", OP); ok(r["statusCode"] == 200 and "Vincula tu aerotermia" in r["body"], "login page in Spanish")
r = http("GET", "/authorize", OP, headers={"accept-language": "en-GB"}); ok("Link your heat pump" in r["body"], "login page in English")
r = http("GET", "/authorize", {**OP, "redirect_uri": "https://evil.com/api/skill/link/x"}); ok(r["statusCode"] == 400, "bad redirect rejected")
r = http("GET", "/authorize", {**OP, "client_id": "x"}); ok(r["statusCode"] == 400, "bad client_id rejected")
r = http("POST", "/authorize", form={**OP, "email": "Ferran@X.com", "password": "bad"}); ok("rechazado" in r["body"], "wrong password message")
r = http("POST", "/authorize", form={**OP, "email": "Ferran@X.com", "password": "good"}, b64=True)
ok(r["statusCode"] == 302, "login redirects to Alexa")
loc = urllib.parse.urlparse(r["headers"]["Location"]); q = dict(urllib.parse.parse_qsl(loc.query))
ok(loc.netloc == "layla.amazon.com" and q["state"] == "st8" and q["code"], "redirect has state+code")
uid = L.user_id_for("ferran@x.com")
u = TABLE.d[f"user#{uid}"]; ok("PANA-RT-1" not in json.dumps(u) and L.fernet().decrypt(u["rt"].encode()) == b"PANA-RT-1", "Panasonic token stored encrypted")
ok(not any("good" in json.dumps(v) for v in TABLE.d.values()), "password never stored")
r = http("POST", "/token", form={"grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT}, headers=basic("x", "y"))
ok(r["statusCode"] == 401, "token endpoint rejects bad client")
r = http("POST", "/token", form={"grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT}, headers=basic())
tok = json.loads(r["body"]); ok(r["statusCode"] == 200 and tok["access_token"] and tok["refresh_token"], "code exchanged for tokens (Basic auth)")
r = http("POST", "/token", form={"grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT}, headers=basic())
ok(r["statusCode"] == 400, "code cannot be reused")
AT = tok["access_token"]

# ---------------- Smart Home ----------------
r = directive("Alexa.Discovery", "Discover", AT)
eps = r["event"]["payload"]["endpoints"]; ids = [e["endpointId"] for e in eps]
ok(ids == ["B944201905_cool", "B944201905_heat", "B944201905_dhw"], f"discovery 3 endpoints {ids}")
ok(eps[0]["friendlyName"] == "Aerotermia Frío" and eps[2]["displayCategories"] == ["WATER_HEATER"], "names/categories")
ok(L.fernet().decrypt(TABLE.d[f"user#{uid}"]["rt"].encode()) == b"PANA-RT-2", "rotated Panasonic token saved")

r = directive("Alexa", "ReportState", AT, "B944201905_cool")
ok(r["event"]["header"]["name"] == "StateReport" and r["context"]["properties"][0]["value"] == "OFF", "report cool OFF")
r = directive("Alexa", "ReportState", AT, "B944201905_dhw"); ok(r["context"]["properties"][0]["value"] == "ON", "report dhw ON")

r = directive("Alexa.PowerController", "TurnOn", AT, "B944201905_cool")
ok(r["event"]["header"]["name"] == "Response" and DEV.zones[1].operation_status == O.ON and DEV.mode == X.COOL
   and DEV.tank.operation_status == O.ON, "cool ON: zone ON, COOL, tank untouched")
ok(DEV.posts[-1] == (U.COOL, {1: O.ON}, O.ON, O.ON), f"cool ON request {DEV.posts[-1]}")
n = len(DEV.posts); directive("Alexa.PowerController", "TurnOff", AT, "B944201905_heat")
ok(len(DEV.posts) == n and DEV.zones[1].operation_status == O.ON, "heat OFF while cooling does nothing")
directive("Alexa.PowerController", "TurnOff", AT, "B944201905_cool")
ok(DEV.zones[1].operation_status == O.OFF and DEV.tank.operation_status == O.ON and DEV.op == O.ON, "cool OFF: zone OFF, tank ON, unit ON")
ok(DEV.posts[-1] == (U.COOL, {1: O.OFF}, O.ON, O.ON), f"cool OFF request mirrors tested one {DEV.posts[-1]}")
directive("Alexa.PowerController", "TurnOn", AT, "B944201905_heat")
ok(DEV.mode == X.HEAT and DEV.zones[1].operation_status == O.ON, "heat ON switches mode to HEAT")
directive("Alexa.PowerController", "TurnOff", AT, "B944201905_heat")
directive("Alexa.PowerController", "TurnOff", AT, "B944201905_dhw")
ok(DEV.tank.operation_status == O.OFF and DEV.op == O.OFF and DEV.posts[-1][0] == U.OFF, "dhw OFF with zones off -> unit OFF")
directive("Alexa.PowerController", "TurnOn", AT, "B944201905_dhw")
ok(DEV.tank.operation_status == O.ON and DEV.op == O.ON and DEV.zones[1].operation_status == O.OFF, "dhw ON, zones untouched")
r = directive("Alexa.PowerController", "TurnOn", AT, "OTHER_cool"); ok(r["event"]["payload"]["type"] == "NO_SUCH_ENDPOINT", "unknown device")
r = directive("Alexa.PowerController", "TurnOn", "nope", "B944201905_cool"); ok(r["event"]["payload"]["type"] == "INVALID_AUTHORIZATION_CREDENTIAL", "unknown token")
TABLE.d[f"access#{L.sha(AT)}"]["ttl"] = int(time.time()) - 1
r = directive("Alexa.PowerController", "TurnOn", AT, "B944201905_cool"); ok(r["event"]["payload"]["type"] == "EXPIRED_AUTHORIZATION_CREDENTIAL", "expired token")
r = http("POST", "/token", form={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                  "client_id": "alexa-skill", "client_secret": "s3cr3t"})
tok2 = json.loads(r["body"]); ok(r["statusCode"] == 200 and tok2["refresh_token"] == tok["refresh_token"], "refresh grant (credentials in body)")
AT = tok2["access_token"]
r = directive("Alexa.Authorization", "AcceptGrant", AT); ok(r["event"]["header"]["name"] == "AcceptGrant.Response", "AcceptGrant")

# temporary Panasonic problem must not unlink
L._sessions.clear(); STATE["version_rejected"] = True
r = directive("Alexa", "ReportState", AT, "B944201905_cool")
ok(r["event"]["payload"]["type"] == "ENDPOINT_UNREACHABLE" and not TABLE.d[f"user#{uid}"]["revoked"], "app-version problem keeps user linked")
STATE["version_rejected"] = False

# Panasonic refresh token dead -> unlink
L._sessions.clear(); STATE["refresh_ok"] = False
r = directive("Alexa", "ReportState", AT, "B944201905_cool")
ok(r["event"]["payload"]["type"] == "INVALID_AUTHORIZATION_CREDENTIAL" and TABLE.d[f"user#{uid}"]["revoked"], "dead Panasonic token revokes user")
r = http("POST", "/token", form={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, headers=basic())
ok(r["statusCode"] == 400, "revoked user cannot refresh -> Alexa asks to relink")
STATE["refresh_ok"] = True

# ---------------- MFA flow ----------------
STATE["mfa"] = True
r = http("POST", "/authorize", form={**OP, "email": "vecino@x.com", "password": "good"})
ok("+34*****12" in r["body"] and "Código" in r["body"], "MFA page shown")
nonce = re.search(r'name="nonce" value="([^"]+)"', r["body"]).group(1)
r = http("POST", "/authorize/mfa", form={"nonce": nonce, "code": "000000"}); ok("incorrecto" in r["body"], "wrong MFA code")
r = http("POST", "/authorize/mfa", form={"nonce": nonce, "code": "123456"}); ok(r["statusCode"] == 302, "MFA ok -> redirect")
u2 = TABLE.d[f"user#{L.user_id_for('vecino@x.com')}"]; ok(L.fernet().decrypt(u2["rt"].encode()) == b"PANA-RT-MFA", "MFA user stored")
r = http("POST", "/authorize/mfa", form={"nonce": nonce, "code": "123456"}); ok("tardado" in r["body"], "nonce single-use")
ok(http("GET", "/nope")["statusCode"] == 404, "404")
print("\nFAILS:", ok.fails)
