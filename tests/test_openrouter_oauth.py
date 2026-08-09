from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer

from scripts.connect_openrouter_oauth import OAuthCallbackHandler


def test_favicon_request_does_not_erase_oauth_code() -> None:
    OAuthCallbackHandler.code = None
    OAuthCallbackHandler.error = None
    OAuthCallbackHandler.completed.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base_url}/callback?code=auth-code", timeout=5):
            pass
        with urllib.request.urlopen(f"{base_url}/favicon.ico", timeout=5) as response:
            assert response.status == 204
    finally:
        server.shutdown()
        server.server_close()

    assert OAuthCallbackHandler.code == "auth-code"
    assert OAuthCallbackHandler.error is None
    assert OAuthCallbackHandler.completed.is_set()
