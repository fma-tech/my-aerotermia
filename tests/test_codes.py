"""Offline tests for the community code and the simulated reviewer heat pump."""
import base64, json, os, re, sys, urllib.parse
from cryptography.fernet import Fernet

os.environ.update(FERNET_KEY=Fernet.generate_key().decode(), OAUTH_CLIENT_ID="alexa-skill",
                  OAUTH_CLIENT_SECRET="s3cr3t", TABLE_NAME="t", AWS_DEFAULT_REGION="eu-west-1",
                  INVITE_CODE="Nou Montcada", REVIEW_CODE="AMAZON-REVIEW-7Q2X")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lambda_function as L


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

LOGINS = []
class FakeClient:  # real Panasonic client must never be used without the community code
    def __init__(s, **kw): LOGINS.append(kw.get("username")); s.refresh_token = "RT"
    async def login(s): pass
    async def get_devices(s): return [object()]
L.Client = FakeClient

REDIRECT = "https://layla.amazon.com/api/skill/link/M2TW2ZU8K9B8SH"
OP = {"client_id": "alexa-skill", "redirect_uri": REDIRECT, "state": "st", "response_type": "code"}
def http(method, path, query=None, form=None, headers=None):
    ev = {"rawPath": path, "requestContext": {"http": {"method": method}},
          "headers": headers or {"accept-language": "es-ES"}, "queryStringParameters": query,
          "body": urllib.parse.urlencode(form) if form else "", "isBase64Encoded": False}
    return L.lambda_handler(ev, None)
def token_for(resp):
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(resp["headers"]["Location"]).query))["code"]
    auth = "Basic " + base64.b64encode(b"alexa-skill:s3cr3t").decode()
    r = http("POST", "/token", form={"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": REDIRECT}, headers={"authorization": auth})
    return json.loads(r["body"])["access_token"]
def directive(ns, name, token, endpoint=None):
    d = {"header": {"namespace": ns, "name": name, "payloadVersion": "3", "messageId": "m",
                    "correlationToken": "c"}, "payload": {}}
    if endpoint: d["endpoint"] = {"endpointId": endpoint, "scope": {"type": "BearerToken", "token": token}}
    else: d["payload"] = {"scope": {"type": "BearerToken", "token": token}}
    return L.lambda_handler({"directive": d}, None)
fails = 0
def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += 0 if c else 1

r = http("GET", "/authorize", OP)
ok("Código de la comunidad" in r["body"] and 'action="/authorize/code"' in r["body"], "code page shown first")
r = http("POST", "/authorize/code", form={**OP, "invite": "wrong"})
ok("Código no válido" in r["body"], "wrong community code rejected")
r = http("POST", "/authorize/code", form={**OP, "invite": "nou-montcada"})
ok("Vincula tu aerotermia" in r["body"] and 'name="invite" value="nou-montcada"' in r["body"],
   "community code accepted (case/spaces/dashes ignored) -> Panasonic sign-in")
r = http("POST", "/authorize", form={**OP, "email": "x@y.com", "password": "p"})
ok("Código de la comunidad" in r["body"] and not LOGINS, "direct POST without code cannot reach Panasonic")
r = http("POST", "/authorize", form={**OP, "invite": "NOU MONTCADA", "email": "x@y.com", "password": "p"})
ok(r["statusCode"] == 302 and LOGINS == ["x@y.com"], "sign-in with valid code links the account")

# reviewer flow
r = http("POST", "/authorize/code", form={**OP, "invite": "amazon-review-7q2x"})
ok(r["statusCode"] == 302, "reviewer code links without Panasonic")
AT = token_for(r)
r = directive("Alexa.Discovery", "Discover", AT)
ids = [e["endpointId"] for e in r["event"]["payload"]["endpoints"]]
ok(ids == ["DEMO000001_cool", "DEMO000001_heat", "DEMO000001_dhw"], f"demo discovery {ids}")
st = lambda e: directive("Alexa", "ReportState", AT, e)["context"]["properties"][0]["value"]
ok(st("DEMO000001_cool") == "OFF" and st("DEMO000001_dhw") == "ON", "demo initial state")
directive("Alexa.PowerController", "TurnOn", AT, "DEMO000001_cool")
ok(st("DEMO000001_cool") == "ON" and st("DEMO000001_dhw") == "ON", "demo cool ON keeps hot water")
directive("Alexa.PowerController", "TurnOn", AT, "DEMO000001_heat")
ok(st("DEMO000001_heat") == "ON" and st("DEMO000001_cool") == "OFF", "demo heat ON switches mode")
directive("Alexa.PowerController", "TurnOff", AT, "DEMO000001_heat")
directive("Alexa.PowerController", "TurnOff", AT, "DEMO000001_dhw")
ok(st("DEMO000001_heat") == "OFF" and st("DEMO000001_dhw") == "OFF", "demo all off")
directive("Alexa.PowerController", "TurnOn", AT, "DEMO000001_dhw")
ok(st("DEMO000001_dhw") == "ON", "demo hot water back on")
ok(LOGINS == ["x@y.com"], "reviewer never touched Panasonic")
print("\nFAILS:", fails)
