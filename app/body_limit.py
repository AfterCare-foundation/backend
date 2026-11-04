# app/body_limit.py
#
# Cap every request body; anything bigger is refused before pydantic or the
# database see it. Sized for fixed-size messages (380 chars): 100 deliveries
# are about 50 KB. A /notify using today's 4096-char payload limit could
# exceed it; real payloads are far smaller, and that limit drops with padding.

MAX_REQUEST_BODY = 128 * 1024

_REJECTION = b'{"detail":"body_too_large"}'


async def _send_too_large(send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_REJECTION)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _REJECTION})


def _declared_length(scope) -> int | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


class LimitRequestBody:
    """
    Pure ASGI middleware. A declared Content-Length over the cap is refused
    without reading anything. Otherwise the body is read (counting real
    bytes, so a short or missing Content-Length proves nothing) up to cap+1
    bytes, then handed to the app as one buffered message. Never raises and
    never logs: nothing here may touch the body, URL, headers or client.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > MAX_REQUEST_BODY:
            await _send_too_large(send)
            return

        chunks = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_REQUEST_BODY:
                await _send_too_large(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        buffered = b"".join(chunks)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
