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


class AsyncFakeRedis:
    """Small async Redis double for atomic-memory-store behavior tests."""

    def __init__(self) -> None:
        import asyncio

        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def eval(self, script: str, numkeys: int, *args: str) -> list[str]:
        keys = args[:numkeys]
        values = args[numkeys:]
        async with self._lock:
            if "dream:reserve-event" in script:
                sequence_key, event_key = keys
                digest, event_ttl, sequence_ttl = values
                record = self.hashes.get(event_key)
                if record is not None:
                    if record["digest"] != digest:
                        return ["conflict", "0"]
                    return [record["status"], record["sequence"]]
                sequence = int(self.values.get(sequence_key, "0")) + 1
                self.values[sequence_key] = str(sequence)
                self.hashes[event_key] = {
                    "digest": digest,
                    "status": "pending",
                    "sequence": str(sequence),
                }
                self.ttls[event_key] = int(event_ttl)
                self.ttls[sequence_key] = int(sequence_ttl)
                return ["reserved", str(sequence)]
            if "dream:commit-event" in script:
                sequence_key, messages_key, summary_key, event_key = keys
                event_json, sequence, ttl = values
                record = self.hashes.get(event_key)
                if record is None:
                    return ["missing"]
                if record["sequence"] != sequence:
                    return ["sequence_conflict"]
                if record["status"] == "committed":
                    return ["duplicate"]
                self.lists.setdefault(messages_key, []).append(event_json)
                record["status"] = "committed"
                for key in (messages_key, summary_key, event_key, sequence_key):
                    if key in self.lists or key in self.values or key in self.hashes:
                        self.ttls[key] = int(ttl)
                return ["committed"]
            if "dream:compare-and-set-envelope" in script:
                summary_key = keys[0]
                expected_version, serialized, ttl = values
                current = self.values.get(summary_key)
                if current is not None:
                    import json

                    if str(json.loads(current)["version"]) != expected_version:
                        return ["0"]
                elif expected_version != "0":
                    return ["0"]
                self.values[summary_key] = serialized
                self.ttls[summary_key] = int(ttl)
                return ["1"]
            if "dream:release-compression-lease" in script:
                key = keys[0]
                if self.values.get(key) != values[0]:
                    return ["0"]
                self.values.pop(key, None)
                self.ttls.pop(key, None)
                return ["1"]
        raise AssertionError("unsupported Lua script")

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        start = max(0, len(values) + start) if start < 0 else start
        end = len(values) - 1 if end == -1 else end
        return values[start : end + 1]

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
    ) -> bool | None:
        async with self._lock:
            if nx and (key in self.values or key in self.lists or key in self.hashes):
                return None
            self.values[key] = value
            if px is not None:
                self.ttls[key] = px // 1000
            return True
