import asyncio

class FakeRemoteCallable:
    """Simulates Ray's remote() call pattern"""
    def __init__(self, fn):
        self._fn = fn

    async def remote(self, *args, **kwargs):
        res = self._fn(*args, **kwargs)
        if asyncio.iscoroutine(res):
            return await res
        return res


class FakeHandle:
    """
    Fake Ray handle for testing without Ray runtime.
    Usage: handle = FakeHandle(embed=lambda q: [0.1, 0.2])
    Then: await handle.embed.remote(q)
    """
    def __init__(self, **methods):
        for name, fn in methods.items():
            setattr(self, name, FakeRemoteCallable(fn))