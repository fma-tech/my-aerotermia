"""Offline tests for personal household codes, admin actions and the reviewer demo."""
import base64, json, os, sys, urllib.parse
from cryptography.fernet import Fernet

os.environ.update(FERNET_KEY=Fernet.generate_key().decode(), OAUTH_CLIENT_ID="alexa-skill",
                  OAUTH_CLIENT_SECRET="s3cr3t", TABLE_NAME="t", AWS_DEFAULT_REGION="eu-west-1",
                  REVIEW_CODE="AMAZON-REVIEW-7Q2X")
os.environ.pop("INVITE_CODE", None); os.environ.pop("OPEN_SIGNUP", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lambda_function as L


class Table:
    def __init__(s): s.d = {}
    def put_item(s, Item): s.d[Item["pk"]] = dict(Item)
    def get_item(s, Key):
        i = s.d.get(Key["pk"]); return {"Item": dict(i)} if i else {}
    def delete_item(s, Key): s.d.pop(Key["pk"], None)
    def update_item(s, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        it = s.d.setdefault(Key["pk"], {"pk": Key["pk"]})
        for part in UpdateExpression[4:].split(","):
            k, v = [x.strip() for x in part.split("=")]
            it[(ExpressionAttributeNames or {}).get(k, k)] = ExpressionAttributeValues[v]
TABLE = Table(); L._table = TABLE

LOGINS = []
class FakeClient:  # a real household; devices simulated with the demo device logic
    def __init__(s, **kw): s.u = kw.get("username"); s.refresh_token = "RT"
    async def login(s):
        if s.u: LOGINS.append(s.u)
    get_devices = L.DemoClient.get_devices
    get_device = L.DemoClient.get_device
    post_device_operation_update = L.DemoClient.post_device_operation_update
L.Client = FakeClient

REDIRECT = "https://layla.amazon.com/api/skill/link/M2TW2ZU8K9B8SH"
OP = {"client_id": "alexa-skill", "redirect_uri": REDIRECT, "state": "st", "response_type": "code"}
def http(method, path, query=None, form=None, headers=None):
    ev = {"rawPath": path, "requestContext": {"http": {"method": method}},
          "headers": headers or {"accept-language": "es-ES"}, "queryStringParameters": query,
          "body": urllib.parse.urlencode(form) if form else "", "isBase64Encoded": False}
    return L.lambda_handler(ev, None)
AUTH = {"authorization": "Basic " + base64.b64encode(b"alexa-skill:s3cr3t").decode()}
def tokens_for(resp):
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(resp["headers"]["Location"]).query))["code"]
    r = http("POST", "/token", form={"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": REDIRECT}, headers=AUTH)
    return json.loads(r["body"])
def directive(ns, name, token, endpoint=None):
    d = {"header": {"namespace": ns, "name": name, "payloadVersion": "3", "messageId": "m",
                    "correlationToken": "c"}, "payload": {}}
    if endpoint: d["endpoint"] = {"endpointId": endpoint, "scope": {"type": "BearerToken", "token": token}}
    else: d["payload"] = {"scope": {"type": "BearerToken", "token": token}}
    return L.lambda_handler({"directive": d}, None)
def link(code, email):
    return http("POST", "/authorize", form={**OP, "invite": code, "email": email, "password": "p"})
admin = lambda **a: L.lambda_handler({"admin": a}, None)
fails = 0
def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += 0 if c else 1

r = http("GET", "/authorize", OP)
ok("Código de acceso" in r["body"], "code page shown first")
ok("Código no válido" in http("POST", "/authorize/code", form={**OP, "invite": "1234567890"})["body"],
   "unknown code rejected")
ok("Código de acceso" in link("", "x@y.com")["body"] and not LOGINS,
   "Panasonic sign-in unreachable without a code")

r = admin(action="create", labels=["3º 2ª", "Bajo 1ª"])
A, B = r["created"][0]["code"], r["created"][1]["code"]
ok(r["ok"] and len(A) == 10 and A.isdigit() and A != B, f"admin created codes {A} {B}")
ok(not any(A in json.dumps(v) for v in TABLE.d.values()), "codes stored hashed/encrypted only")

r = http("POST", "/authorize/code", form={**OP, "invite": f"{A[:3]}-{A[3:6]} {A[6:]}"})
ok("Vincula tu aerotermia" in r["body"], "personal code accepted (dashes/spaces ignored)")
r = link(A, "ana@x.com"); ok(r["statusCode"] == 302, "household A linked")
TA = tokens_for(r)
r = link(A, "otro@x.com")
ok("ya se ha usado" in r["body"], "code A refused for a different Panasonic account")
ok(link(A, "ANA@x.com")["statusCode"] == 302, "same household can re-link with its code")

lst = admin(action="list")
ok(lst["total"] == 2 and lst["used"] == 1 and
   {c["label"]: c["status"] for c in lst["codes"]} == {"3º 2ª": "used", "Bajo 1ª": "unused"}
   and any(c["code"] == A and c["email"] == "ana@x.com" for c in lst["codes"]), "admin list")

st = lambda tok: directive("Alexa", "ReportState", tok, "DEMO000001_dhw")
ok(st(TA["access_token"])["event"]["header"]["name"] == "StateReport", "household A works")
ok(admin(action="revoke", label="3º 2ª")["ok"], "revoke by label")
r = st(TA["access_token"])
ok(r["event"]["payload"].get("type") == "INVALID_AUTHORIZATION_CREDENTIAL", "revoked household stops working")
r = http("POST", "/token", form={"grant_type": "refresh_token", "refresh_token": TA["refresh_token"]}, headers=AUTH)
ok(r["statusCode"] == 400, "revoked household cannot refresh tokens")
ok("Código no válido" in http("POST", "/authorize/code", form={**OP, "invite": A})["body"], "revoked code rejected")
ok(admin(action="restore", code=A)["ok"] and
   st(TA["access_token"])["event"]["header"]["name"] == "StateReport", "restore brings it back")

ok(admin(action="reset", code=A)["ok"], "reset code A")
ok(link(A, "otro@x.com")["statusCode"] == 302, "after reset, code A works for a new account")

r = http("POST", "/authorize/code", form={**OP, "invite": "amazon-review-7q2x"})
ok(r["statusCode"] == 302, "reviewer code still links the simulated heat pump")
ok(L.lambda_handler({"admin": {"action": "list"}, "requestContext": {"http": {"method": "GET"}},
                     "rawPath": "/", "headers": {}}, None).get("statusCode") == 200,
   "admin actions not reachable through the Function URL")
# create is idempotent per label; delete by prefix / labels / code
r = admin(action="create", labels=["3º 2ª", "Ático"])
ok(r["skipped_existing"] == ["3º 2ª"] and [c["label"] for c in r["created"]] == ["Ático"],
   "create skips households that already have a code")
r = admin(action="create", count=3)
ok([c["label"] for c in r["created"]] == ["code-1", "code-2", "code-3"], "count creates code-N labels")
r = admin(action="create", count=2)
ok([c["label"] for c in r["created"]] == ["code-4", "code-5"], "count never reuses code-N labels")
r = admin(action="delete", label_prefix="code-")
ok(r["ok"] and sorted(r["deleted"]) == ["code-1", "code-2", "code-3", "code-4", "code-5"], "delete by prefix")
ok(all(not lb.startswith("code-") for lb in L._invite_index().values()), "deleted codes gone from index")
TB = tokens_for(link(B, "berta@x.com"))
r = admin(action="delete", labels=["Bajo 1ª", "Ático"])
ok(r["ok"] and len(r["deleted"]) == 2, "delete by labels")
r = directive("Alexa", "ReportState", TB["access_token"], "DEMO000001_dhw")
ok(r["event"]["payload"].get("type") == "INVALID_AUTHORIZATION_CREDENTIAL", "deleting a used code cuts that household")
ok("Código no válido" in http("POST", "/authorize/code", form={**OP, "invite": B})["body"], "deleted code no longer valid")
ok(admin(action="list")["total"] == 1, "only one code left")
admin(action="create", count=4)
ok(admin(action="delete", all="yes")["ok"] is False, "delete all needs all: true (boolean)")
r = admin(action="delete", all=True)
ok(r["ok"] and len(r["deleted"]) == 5 and admin(action="list")["total"] == 0, "delete all")
print("\nFAILS:", fails)
