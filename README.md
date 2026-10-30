# My Aerotermia

Alexa Smart Home skill to control **Panasonic Aquarea** air-to-water heat pumps
(with the **CZ-TAW1B** cloud adapter) through the Panasonic Comfort Cloud.

The official Panasonic Comfort Cloud Alexa skill only discovers air conditioners,
not Aquarea heat pumps. This skill fills that gap.

> Unofficial project. Not affiliated with or endorsed by Panasonic.
> It uses the community library [aioaquarea-ng](https://pypi.org/project/aioaquarea-ng/),
> which talks to an undocumented Panasonic API that may change without notice.

## What users get

After linking their Panasonic account in the Alexa app, each user gets up to three devices:

| Alexa device | Turn on | Turn off |
|---|---|---|
| **Aerotermia Frío** (cooling) | Zones ON in COOL mode | Zones OFF, hot water tank untouched |
| **Aerotermia Calor** (heating) | Zones ON in HEAT mode | Zones OFF, hot water tank untouched |
| **Aerotermia Agua Caliente** (DHW) | Tank ON | Tank OFF, zones untouched |

Turning *cooling* off while the unit is *heating* (or vice versa) does nothing.
Users can rename the devices and use them in their own Alexa routines.

Example routine: *"Alexa, apaga el aire acondicionado"* → turn off the living-room
thermostat + turn off **Aerotermia Frío**.

## How it works

A single AWS Lambda function (Python 3.13, `eu-west-1`) handles:

- **Alexa Smart Home directives** (trigger: Alexa Smart Home): Discovery,
  `Alexa.PowerController` TurnOn/TurnOff, ReportState, AcceptGrant.
- **OAuth 2.0 account linking** (trigger: Lambda Function URL):
  - `GET/POST /authorize`: Panasonic sign-in page (with SMS / authenticator MFA support)
  - `POST /authorize/mfa`: MFA code
  - `POST /token`: `authorization_code` and `refresh_token` grants

Storage is one DynamoDB table (`pk` string partition key, TTL on `ttl`):

| Item | Content |
|---|---|
| `user#<id>` | Panasonic refresh token, encrypted with Fernet |
| `code#<sha256>` / `access#<sha256>` / `refresh#<sha256>` | Our OAuth codes/tokens, stored hashed |

**Panasonic passwords are never stored.**

## Repository layout

```
src/lambda_function.py   Lambda code (Alexa + OAuth)
tests/test_lambda.py     Offline tests with fake Panasonic + DynamoDB
tools/aquarea_test.py    Read-only connection test against your real unit
tools/aquarea_control_test.py  Control test (cool on/off) that restores the initial state
build.sh                 Builds dist/my-aerotermia-lambda.zip
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

python tests/test_lambda.py        # offline tests
python tools/aquarea_test.py       # read-only test against your Panasonic account
./build.sh                         # -> dist/my-aerotermia-lambda.zip
```

## Deployment (AWS, region Europe (Ireland) eu-west-1)

1. **DynamoDB**: table `aquarea-alexa`, partition key `pk` (String). Enable TTL on attribute `ttl`.
2. **Lambda**: function `aquarea-alexa`, Python 3.13, x86_64, 512 MB, 20 s timeout.
   Upload `dist/my-aerotermia-lambda.zip`.
3. **Environment variables**:

   | Key | Value |
   |---|---|
   | `TABLE_NAME` | `aquarea-alexa` |
   | `FERNET_KEY` | `python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
   | `OAUTH_CLIENT_ID` | `my-aerotermia-alexa` |
   | `OAUTH_CLIENT_SECRET` | `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
   | `NAME_COOL`, `NAME_HEAT`, `NAME_DHW` | optional device names |

4. **IAM**: inline policy on the execution role allowing `dynamodb:GetItem`, `PutItem`,
   `UpdateItem`, `DeleteItem` on `arn:aws:dynamodb:eu-west-1:*:table/aquarea-alexa`.
5. **Function URL**: auth type `NONE`.
6. **Trigger**: Alexa Smart Home, with the skill ID.
7. **Alexa developer console** (Smart Home skill):
   - Default endpoint: the Lambda ARN.
   - Account linking: Auth Code Grant, Authorization URI `<function-url>/authorize`,
     Access Token URI `<function-url>/token`, Client ID `my-aerotermia-alexa`,
     Client Secret = `OAUTH_CLIENT_SECRET`, authentication scheme HTTP Basic, scope `aquarea`.

Never commit secrets: `FERNET_KEY`, `OAUTH_CLIENT_SECRET` and `.aquarea_token` files
belong only in AWS / your machine.
