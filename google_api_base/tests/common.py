import json


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        *,
        payload=None,
        content=None,
        headers=None,
        stream_error=None,
    ):
        self.status_code = status_code
        self.headers = dict(headers or {})
        if content is None:
            content = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.content = content
        self.stream_error = stream_error
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size=16 * 1024):
        self.iterated = True
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]
        if self.stream_error:
            raise self.stream_error

    def close(self):
        self.closed = True
