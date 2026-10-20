#!/usr/bin/env python3
"""Send a one-off QQ notice through NapCat, with AstrBot already stopped.

Why this exists
---------------
The normal way to reach a user is AstrBot: it owns the reply path, so the Runtime
only ever fills an outbox. Once AstrBot is stopped there is no sender left -- and
"the service is gone" is exactly the message that has to arrive *after* it stops,
not before. NapCat is still logged in, so the QQ account can still talk; it just
has no API to talk through.

So this script opens one, on a port that is never published:

1. log into the NapCat WebUI (``POST /api/auth/login`` with ``sha256(token + ".napcat")``);
2. read the current OneBot config and add a loopback-side HTTP server entry;
3. push it back with ``/api/OB11Config/SetConfig`` -- NapCat hot-reloads its network
   layer, the QQ session is *not* restarted, so no QR scan is needed;
4. send ``send_private_msg`` to each id from ``--ids-file``;
5. restore the previous config, removing the server again.

The port is only reachable on the container network (nothing is published), the
token is random per run, and the restored config is byte-identical to the original,
so the account is not left with an extra listener.

Ids come from a file, never from the command line: a QQ number in a shell history
or a process list is a leak, and this script is meant to be runnable on a host that
other people share.

Usage
-----
    python3 scripts/napcat_notice.py \
        --webui http://127.0.0.1:6098 --token-file ~/.napcat-webui-token \
        --container xxj-napcat-test \
        --text "「通知」服务已下线。" --ids-file /tmp/notice-ids.txt

    # rehearse the whole wiring without sending or changing anything
    python3 scripts/napcat_notice.py ... --dry-run

Exit status: 0 every message accepted by NapCat, 1 at least one failed, 2 the
wiring could not be established (nothing was sent).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _post(url: str, payload: Any, credential: str = "", timeout: float = 20.0) -> tuple[int, Any]:
    """POST JSON and return ``(status, decoded)``; HTTP errors are returned, not raised."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, {"_raw": body[:200]}
    except urllib.error.HTTPError as exc:
        return exc.code, {"_raw": exc.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:  # noqa: BLE001 - the caller decides what a failure means
        return 0, {"_error": f"{type(exc).__name__}: {exc}"}


def _mask(user_id: int) -> str:
    """Mask an id for stdout: enough to tell rows apart, not enough to reuse."""
    text = str(user_id)
    if len(text) <= 5:
        return "*" * len(text)
    return f"{text[:3]}{'*' * (len(text) - 5)}{text[-2:]}"


def _read_token(path: str, literal: str) -> str:
    """Read the WebUI token from a file (preferred) or a literal."""
    if path:
        with open(path, "r", encoding="utf-8-sig") as handle:
            token = handle.read().strip()
        if token:
            return token
        raise SystemExit(f"token file {path} is empty")
    if literal:
        return literal
    raise SystemExit("give --token-file (preferred) or --token")


def _read_ids(path: str) -> list[int]:
    """Read one QQ id per line, ignoring blanks and comments."""
    ids: list[int] = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                ids.append(int(line))
            except ValueError:
                raise SystemExit(f"not a QQ id on this line: {line[:4]}...")
    if not ids:
        raise SystemExit(f"no ids in {path}")
    return ids


def _container_ip(container: str) -> str:
    """Return the first bridge address of ``container`` (docker must be on PATH)."""
    result = subprocess.run(
        ["docker", "inspect", container, "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"docker inspect {container} failed: {result.stderr.strip()[:200]}")
    for candidate in result.stdout.split():
        if candidate:
            return candidate
    raise SystemExit(f"{container} has no bridge address")


def _wait_onebot(base: str, token: str, timeout: float = 15.0) -> bool:
    """Poll ``/get_login_info`` until the freshly added HTTP server answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, payload = _post(f"{base}/get_login_info", {}, credential=token, timeout=5.0)
        if status == 200 and isinstance(payload, dict) and payload.get("status") in ("ok", "async"):
            return True
        time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    """Wire one temporary OneBot endpoint, send the notice, then restore the config."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--webui", required=True, help="NapCat WebUI base, e.g. http://127.0.0.1:6098")
    parser.add_argument("--token-file", default="", help="file holding the WebUI token (preferred)")
    parser.add_argument("--token", default="", help="WebUI token literal (avoid: leaks into ps/history)")
    parser.add_argument("--container", default="", help="container name, used to resolve its bridge address")
    parser.add_argument("--onebot-url", default="", help="reachable base for the temporary endpoint")
    parser.add_argument("--port", type=int, default=3099, help="temporary OneBot HTTP port (not published)")
    parser.add_argument("--text", required=True, help="message body sent to every id")
    parser.add_argument("--ids-file", required=True, help="one QQ id per line")
    parser.add_argument("--dry-run", action="store_true", help="log in and plan, change nothing, send nothing")
    parser.add_argument("--keep-server", action="store_true", help="leave the endpoint in place (debugging)")
    args = parser.parse_args(argv)

    webui = args.webui.rstrip("/")
    token = _read_token(args.token_file, args.token)
    ids = _read_ids(args.ids_file)
    # A message piped in from a file written by a Windows editor arrives with a byte-order
    # mark. QQ renders it as nothing but it is still in the text, so strip it here rather
    # than asking every caller to remember (this was found the hard way).
    text = args.text.lstrip("\ufeff").strip()
    if not text:
        raise SystemExit("--text is empty")
    print(f"targets: {len(ids)}   text: {text[:40]}...")

    digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
    status, payload = _post(f"{webui}/api/auth/login", {"hash": digest})
    credential = ""
    if status == 200 and isinstance(payload, dict):
        data = payload.get("data") or {}
        credential = str(data.get("Credential") or data.get("credential") or "")
    if not credential:
        print(f"login failed: HTTP {status} {str(payload)[:160]}")
        return 2
    print("webui login: ok")

    status, payload = _post(f"{webui}/api/OB11Config/GetConfig", {}, credential=credential)
    if status != 200 or not isinstance(payload.get("data"), dict):
        print(f"cannot read the OneBot config: HTTP {status} {str(payload)[:160]}")
        return 2
    original = payload["data"]
    network = dict(original.get("network") or {})
    existing = list(network.get("httpServers") or [])
    already = next((s for s in existing if int(s.get("port") or 0) == args.port), None)

    onebot_token = secrets.token_hex(16)
    entry = already or {
        "name": "notice",
        "enable": True,
        "host": "0.0.0.0",
        "port": args.port,
        "enableCors": False,
        "enableWebsocket": False,
        "messagePostFormat": "string",
        "token": onebot_token,
        "debug": False,
    }
    if already:
        onebot_token = str(already.get("token") or "")
        print(f"http server on {args.port} already configured; reusing it")
    else:
        network["httpServers"] = existing + [entry]
        patched = dict(original)
        patched["network"] = network
        if args.dry_run:
            print(f"[dry-run] would add httpServers entry on port {args.port}")
        else:
            status, payload = _post(
                f"{webui}/api/OB11Config/SetConfig",
                {"config": json.dumps(patched, ensure_ascii=False)},
                credential=credential,
            )
            if status != 200 or (isinstance(payload, dict) and payload.get("code") not in (0, None)):
                print(f"SetConfig refused: HTTP {status} {str(payload)[:200]}")
                return 2
            print(f"http server enabled on port {args.port} (hot reload, QQ session untouched)")

    base = args.onebot_url.rstrip("/") if args.onebot_url else ""
    if not base:
        if not args.container:
            print("give --onebot-url or --container so the endpoint is reachable")
            return 2
        base = f"http://{_container_ip(args.container)}:{args.port}"

    if args.dry_run:
        print(f"[dry-run] would send to {base}/send_private_msg, then restore the config")
        return 0

    if not _wait_onebot(base, onebot_token):
        print(f"the temporary endpoint at {base} never answered; nothing was sent")
        _restore(webui, credential, original, args.keep_server, args.port)
        return 2
    print(f"onebot endpoint: {base}")

    sent, failed = 0, 0
    for user_id in ids:
        status, payload = _post(
            f"{base}/send_private_msg",
            {"user_id": user_id, "message": text},
            credential=onebot_token,
            timeout=25.0,
        )
        ok = status == 200 and isinstance(payload, dict) and payload.get("status") == "ok"
        if ok:
            sent += 1
            print(f"  {_mask(user_id)}  sent (message_id={payload.get('data', {}).get('message_id')})")
        else:
            failed += 1
            print(f"  {_mask(user_id)}  FAILED  HTTP {status} {str(payload)[:120]}")

    _restore(webui, credential, original, args.keep_server, args.port)
    print(f"\nsent: {sent}   failed: {failed}")
    return 0 if failed == 0 else 1


def _restore(webui: str, credential: str, original: dict, keep: bool, port: int) -> None:
    """Put the OneBot config back exactly as it was found."""
    if keep:
        print(f"leaving the endpoint on port {port} in place (--keep-server)")
        return
    status, payload = _post(
        f"{webui}/api/OB11Config/SetConfig",
        {"config": json.dumps(original, ensure_ascii=False)},
        credential=credential,
    )
    ok = status == 200 and isinstance(payload, dict) and payload.get("code") in (0, None)
    print("config restored" if ok else f"! config restore failed: HTTP {status} {str(payload)[:160]}")


if __name__ == "__main__":
    sys.exit(main())
