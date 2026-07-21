"""
Lightweight fakes for aiohttp.ClientSession, used across tests instead of
mocking real network calls. xomify-backend has no existing test suite for
spotify.py/playlist.py to reuse, so these fakes are new for xomtracks.
"""


class FakeResponse:
    def __init__(self, status: int, json_data: dict, headers: dict | None = None):
        self.status = status
        self._json_data = json_data
        self.headers = headers or {}

    async def json(self):
        return self._json_data

    async def text(self):
        return str(self._json_data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """
    Queue-based fake: each call to .get/.post/.put/.delete pops the next
    queued FakeResponse for that verb (in call order). Also records calls
    for assertion (method, url, kwargs).
    """

    def __init__(self):
        self._queues: dict[str, list[FakeResponse]] = {"get": [], "post": [], "put": [], "delete": []}
        self.calls: list[tuple[str, str, dict]] = []

    def queue(self, method: str, response: FakeResponse):
        self._queues[method.lower()].append(response)

    def _pop(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        queue = self._queues[method.lower()]
        if not queue:
            raise AssertionError(f"No queued FakeResponse for {method.upper()} {url}")
        return queue.pop(0)

    def get(self, url, headers=None, **kwargs):
        return self._pop("get", url, headers=headers, **kwargs)

    def post(self, url, headers=None, json=None, data=None, **kwargs):
        return self._pop("post", url, headers=headers, json=json, data=data, **kwargs)

    def put(self, url, headers=None, json=None, data=None, **kwargs):
        return self._pop("put", url, headers=headers, json=json, data=data, **kwargs)

    def delete(self, url, headers=None, json=None, **kwargs):
        return self._pop("delete", url, headers=headers, json=json, **kwargs)
