"""In-memory stand-ins for Upstash Redis."""

from collections.abc import Sequence

from dota_insights.kv import KV, KVError


class FakeKV(KV):
    """A dict with TTLs, behaving like the Redis commands ``UpstashKV`` sends."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.lists: dict[str, list[str]] = {}
        self.commands: list[str] = []

    async def get(self, key: str) -> str | None:
        self.commands.append("GET")
        return self.data.get(key)

    async def mget(self, keys: Sequence[str]) -> list[str | None]:
        self.commands.append("MGET")
        return [self.data.get(key) for key in keys]

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.commands.append("SET")
        self.data[key], self.ttls[key] = value, ex

    async def set_nx(self, key: str, value: str, *, ex: int) -> bool:
        self.commands.append("SETNX")
        if key in self.data:
            return False
        self.data[key], self.ttls[key] = value, ex
        return True

    async def incr(self, key: str) -> int:
        self.commands.append("INCR")
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    async def incr_windows(self, windows: Sequence[tuple[str, int]]) -> list[int]:
        self.commands.append("EVAL")
        out = []
        for key, ttl in windows:
            self.data[key] = str(int(self.data.get(key, "0")) + 1)
            self.ttls.setdefault(key, ttl)
            out.append(int(self.data[key]))
        return out

    async def push_capped(self, key: str, value: str, *, max_len: int) -> None:
        self.commands.append("RPUSH")
        items = self.lists.setdefault(key, [])
        items.append(value)
        del items[: max(0, len(items) - max_len)]

    async def list_head(self, key: str, count: int) -> list[str]:
        self.commands.append("LRANGE")
        return list(self.lists.get(key, [])[:count])

    async def list_drop(self, key: str, count: int) -> None:
        self.commands.append("LTRIM")
        del self.lists.get(key, [])[:count]

    async def list_len(self, key: str) -> int:
        self.commands.append("LLEN")
        return len(self.lists.get(key, []))


class BrokenKV(KV):
    """Redis that is down: every command fails."""

    def __init__(self) -> None:
        self.calls = 0

    def _fail(self):
        self.calls += 1
        raise KVError("down")

    async def get(self, key):
        self._fail()

    async def mget(self, keys):
        self._fail()

    async def set(self, key, value, *, ex):
        self._fail()

    async def set_nx(self, key, value, *, ex):
        self._fail()

    async def incr(self, key):
        self._fail()

    async def incr_windows(self, windows):
        self._fail()

    async def push_capped(self, key, value, *, max_len):
        self._fail()

    async def list_head(self, key, count):
        self._fail()

    async def list_drop(self, key, count):
        self._fail()

    async def list_len(self, key):
        self._fail()
