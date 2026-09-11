"""Local-only request policy."""

def same_origin(headers):
    host, origin = headers.get("Host", ""), headers.get("Origin")
    hostname = host.rsplit(":", 1)[0].lower()
    return hostname in {"127.0.0.1", "localhost"} and (
        not origin or origin.rstrip("/").lower() == f"http://{host}".lower()
    )


def valid_token(headers, token):
    return headers.get("X-CNS-Token") == token
