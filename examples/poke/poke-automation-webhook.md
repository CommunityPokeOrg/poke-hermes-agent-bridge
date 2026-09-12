# Triggering tasks via /v1/webhooks/poke

`POST /v1/webhooks/poke` accepts a `TaskCreate` JSON body signed with
`BRIDGE_WEBHOOK_SECRET`. Mode is forced to `async`; when `POKE_API_KEY` is set
the callback defaults to `{"type":"poke"}` so results are pushed back to Poke.

Headers:

- `X-Bridge-Timestamp`: unix seconds (±300s)
- `X-Bridge-Signature`: `sha256=<hex hmac-sha256(secret, "<ts>.<raw body>")>`

Signing helper (Python, stdlib only):

```python
import hashlib, hmac, json, time


def signed_request(body: dict, secret: str):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Bridge-Timestamp": ts, "X-Bridge-Signature": f"sha256={sig}"}


# usage:
# raw, headers = signed_request({"prompt": "nightly summary"}, os.environ["BRIDGE_WEBHOOK_SECRET"])
# httpx.post("https://bridge.example.com/v1/webhooks/poke", content=raw, headers=headers)
```

cURL one-liner:

```bash
BODY='{"prompt":"nightly summary"}'
TS=$(date +%s)
SIG=$(printf '%s' "$TS.$BODY" | openssl dgst -sha256 -hmac "$BRIDGE_WEBHOOK_SECRET" | cut -d' ' -f2)
curl -X POST http://127.0.0.1:8700/v1/webhooks/poke \
  -H "X-Bridge-Timestamp: $TS" -H "X-Bridge-Signature: sha256=$SIG" \
  -H 'Content-Type: application/json' -d "$BODY"
```
