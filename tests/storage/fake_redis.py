class FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _queue(self, name: str, *args: object, **kwargs: object) -> "FakePipeline":
        self.commands.append((name, args, kwargs))
        return self

    def rpush(self, key: str, value: str) -> "FakePipeline":
        return self._queue("rpush", key, value)

    def ltrim(self, key: str, start: int, end: int) -> "FakePipeline":
        return self._queue("ltrim", key, start, end)

    def expire(self, key: str, seconds: int) -> "FakePipeline":
        return self._queue("expire", key, seconds)

    def set(self, key: str, value: str, *, ex: int) -> "FakePipeline":
        return self._queue("set", key, value, ex=ex)

    def execute(self) -> list[object]:
        return [
            getattr(self.client, name)(*args, **kwargs)
            for name, args, kwargs in self.commands
        ]


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.expirations: list[tuple[str, int]] = []
        self.pipeline_transactions: list[bool] = []

    def pipeline(self, *, transaction: bool = True) -> FakePipeline:
        self.pipeline_transactions.append(transaction)
        return FakePipeline(self)

    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        start = max(0, len(values) + start) if start < 0 else start
        end = len(values) - 1 if end == -1 else end
        return values[start : end + 1]

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def ltrim(self, key: str, start: int, end: int) -> bool:
        self.lists[key] = self.lrange(key, start, end)
        return True

    def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def exists(self, *keys: str) -> int:
        return sum(key in self.lists or key in self.values for key in keys)

    def expire(self, key: str, seconds: int) -> bool:
        self.expirations.append((key, seconds))
        if key not in self.lists and key not in self.values:
            return False
        self.ttls[key] = seconds
        return True

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(key in self.lists or key in self.values)
            self.lists.pop(key, None)
            self.values.pop(key, None)
            self.ttls.pop(key, None)
        return deleted
