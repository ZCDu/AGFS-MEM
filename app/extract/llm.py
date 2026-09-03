"""
LLM client for the extraction layer.

Speaks the OpenAI chat-completions protocol, which DeepSeek implements, so
the same client works against DeepSeek, OpenAI, Together, Ollama, vLLM and
anything else that copied that shape. Only the base URL and model name
change.

stdlib urllib rather than httpx or the openai package: this makes exactly one
kind of request, and the project has kept its dependency list short
deliberately. A new runtime dependency for a single POST is a poor trade.

DEGRADES, DOES NOT CRASH
    No API key means the extraction layer is disabled and every other part of
    the service keeps working. Memory storage must not depend on a third
    party being reachable — that is the whole point of owning the store.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request

logger = logging.getLogger("memory_backend.llm")


class LLMError(Exception):
    """Anything that stopped us getting a usable response."""


class LLMTruncated(LLMError):
    """The model stopped because it ran out of output tokens.

    Distinct from a parse failure: the JSON is not malformed, it is
    incomplete. Reporting it as "no JSON object found" sent people looking for
    a prompt or schema problem when the fix is more tokens or less input.
    """

    def __init__(self, text: str, max_tokens: int):
        self.text = text
        self.max_tokens = max_tokens
        super().__init__(
            f"The model ran out of room (max_tokens={max_tokens}) and its reply "
            f"was cut off mid-JSON after {len(text)} characters. Raise "
            f"LLM_MAX_TOKENS, or extract a shorter passage of the conversation.")


class LLMNotConfigured(LLMError):
    """No API key. The caller should report this as 503, not 500 — it is a
    deployment state, not a fault."""


# LLMs habitually wrap JSON in markdown fences and prose despite being told
# not to. Stripping that is the caller's job, not something to fail on.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def salvage_truncated_json(text: str) -> dict | None:
    """Recover the complete objects from a reply that was cut off mid-JSON.

    A truncated extraction is usually mostly good: the model emits entities in
    order and the cut lands inside the last one. Discarding the whole reply
    throws away work that was already paid for, and forces the user to retry a
    long conversation with no guarantee it fits the second time either.

    Closes the structure by trimming back to the last complete element of each
    top-level array. Returns None if nothing whole can be recovered.
    """
    start = text.find("{")
    if start == -1:
        return None

    # Walk the text tracking nesting, and remember the position after every
    # element that closed cleanly at array depth.
    depth = 0
    in_str = False
    prev = ""
    last_good: dict[str, int] = {}
    current_key: str | None = None
    key_depth = 0

    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if ch == '"' and prev != "\\":
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            # An object closing at depth 2 is one element of a top-level array.
            if depth == 2 and ch == "}" and current_key:
                last_good[current_key] = i + 1
        prev = ch

        if not in_str and depth == 2 and ch == "[":
            key_depth = depth
        if not in_str and depth <= 1 and ch == '"':
            pass

    # Identify which arrays we can close, by name, from the raw text.
    import re as _re
    rebuilt: dict = {}
    for key in ("entities", "relations"):
        m = _re.search(rf'"{key}"\s*:\s*\[', text)
        if not m:
            continue
        arr_start = m.end()
        elems = []
        depth = 0
        in_str = False
        prev = ""
        elem_start = None
        for i in range(arr_start, len(text)):
            ch = text[i]
            if in_str:
                if ch == '"' and prev != "\\":
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    elem_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and elem_start is not None:
                    try:
                        elems.append(json.loads(text[elem_start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    elem_start = None
            elif ch == "]" and depth == 0:
                break
            prev = ch
        if elems:
            rebuilt[key] = elems

    return rebuilt or None


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response.

    Tries, in order: the whole string, a fenced block, then the outermost
    brace-balanced span. Models are inconsistent about which they produce and
    a rigid parser turns a usable answer into an error.
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("Model returned an empty response.")

    for candidate in _candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMError(f"No JSON object found in model response: {text[:200]!r}")


def _candidates(text: str):
    yield text
    for m in _FENCE.finditer(text):
        yield m.group(1).strip()
    start = text.find("{")
    if start == -1:
        return
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                yield text[start:i + 1]
                return


class LLMClient:
    """Minimal chat-completions client."""

    def __init__(self, api_key: str | None, base_url: str, model: str,
                 timeout: float = 60.0, max_retries: int = 2):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, temperature: float = 0.0,
                 max_tokens: int = 10000, json_mode: bool = True) -> str:
        """One completion. Returns the assistant message text.

        temperature defaults to 0: this is an extraction task with a schema,
        not a creative one, and reproducibility matters more than variety when
        the output is going to mutate a database.

        json_mode defaults to True because extraction is the original caller
        and its output is parsed. Chat must pass json_mode=False — with it on,
        DeepSeek returns a JSON object where the user expects prose, so replies
        arrive as `{"answer": "..."}` or worse.
        """
        if not self.configured:
            raise LLMNotConfigured(
                "No LLM API key configured. Set DEEPSEEK_API_KEY to enable "
                "conversation extraction.")

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            # Honoured by DeepSeek and OpenAI; harmless where it is not.
            body["response_format"] = {"type": "json_object"}
        payload = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload, method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            })

        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                t0 = time.perf_counter()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                ms = (time.perf_counter() - t0) * 1000
                usage = body.get("usage") or {}
                logger.info("llm: %s in %.0fms, %s prompt + %s completion tokens",
                            self.model, ms, usage.get("prompt_tokens", "?"),
                            usage.get("completion_tokens", "?"))
                choices = body.get("choices") or []
                if not choices:
                    raise LLMError(f"Response had no choices: {str(body)[:200]}")
                content = choices[0].get("message", {}).get("content", "") or ""
                # The API says explicitly when it hit the cap. Without checking
                # this, truncation surfaces as an unparseable-JSON error that
                # points at entirely the wrong cause.
                if choices[0].get("finish_reason") == "length":
                    raise LLMTruncated(content, max_tokens)
                return content
            except LLMTruncated:
                # Retrying produces the same truncation and costs another call.
                raise
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # 4xx are permanent: a bad key or a malformed request will not
                # succeed on retry, and retrying wastes the caller's time.
                if 400 <= e.code < 500 and e.code != 429:
                    raise LLMError(f"LLM rejected the request ({e.code}): {detail}") from e
                last = LLMError(f"LLM error {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = LLMError(f"Could not reach the LLM: {type(e).__name__}: {e}")
            except json.JSONDecodeError as e:
                last = LLMError(f"LLM returned invalid JSON envelope: {e}")

            if attempt < self.max_retries:
                time.sleep(1.5 ** attempt)

        raise last or LLMError("LLM call failed for an unknown reason.")

    # ---------- topic system ----------

    _TOPIC_SYSTEM = (
        "You decide whether a message (or meeting summary / project note) "
        "starts a NEW topic that deserves its own knowledge base (wiki), or "
        "belongs to an existing one. You must answer in STRICT JSON with "
        "exactly these keys and no prose outside the JSON:\n"
        '{"is_new_topic": bool, "belongs_to": string|null, "topic": string, '
        '"title": string|null, "description": string, "reason": string}\n'
        "Rules:\n"
        "- is_new_topic: true ONLY when the message is about something "
        "genuinely new and unrelated to every existing wiki's topic (a new "
        "project, a new meeting on a fresh subject, a different domain). "
        "Continue an existing topic -> false.\n"
        "- belongs_to: when is_new_topic is false, the wiki_id of the existing "
        "wiki this best fits, else null.\n"
        "- topic: a very short (3-8 word) abstract noun phrase summarising "
        "the message's subject, e.g. \"SRE on-call rotation planning\".\n"
        "- title: when is_new_topic is true, a concise wiki name for the new "
        "topic (a few words, title case, no articles), else null.\n"
        "- description: one sentence describing the scope of the new wiki.\n"
        "- reason: one short sentence justifying the decision.\n"
        "Deciding -- the single most important rule:\n"
        "- Only classify a message as CONTINUING an existing wiki when the "
        "message points at the SAME concrete subject that wiki already owns: "
        "the same named project, the same named person/team, or the same "
        "specific entity listed in that wiki's entities. You must be able to "
        "name that specific entity in your reason.\n"
        "- Sharing generic business vocabulary (migration, cloud, data, "
        "legacy, rollout, target, team, budget, platform, project) with an "
        "existing wiki is NOT evidence of continuation. Different named "
        "projects/teams/domains are different topics regardless of shared "
        "jargon.\n"
        "- A message can mention a known company/topic in passing without "
        "continuing it. If the message's real subject is not one of the "
        "existing wikis' named subjects, it is a NEW topic, even if it cites "
        "or relates to something a wiki covers.\n"
        "- When in doubt (you cannot name a specific existing entity the "
        "message continues), choose is_new_topic=true. Fragmentation into a "
        "new, correctly-named wiki is better than wrongly merging two "
        "distinct subjects.\n"
        "- The entity lists below matter: compare the message's subject "
        "against the entities the existing wikis actually hold, not just "
        "their names and topic phrases."
    )

    def evaluate_new_topic(self, text: str,
                           existing: list[dict]) -> dict:
        """Ask the model whether this message starts a new topic (and thus
        deserves a new wiki), given the topics of the wikis the caller can
        reach.

        `existing` is a list of {wiki_id, title, topic} for reachable wikis.
        Returns a dict with {is_new_topic, belongs_to, topic, title,
        description, reason}. Degrades to a safe "continue existing" verdict
        if the model is unavailable, so a routing failure never fragments a
        graph and never blocks the caller.
        """
        if not self.configured:
            raise LLMNotConfigured(
                "No LLM API key configured. Set DEEPSEEK_API_KEY to enable "
                "topic-based wiki creation.")

        if not existing:
            items = "(no existing wikis)"
        else:
            items = "\n".join(
                f"- {w['wiki_id']}: title={w['title']!r}, topic={w.get('topic') or ''!r}, "
                f"entities={[e for e in (w.get('sample_entities') or [])][:8]!r}"
                for w in existing)
        # With no existing wikis there is nothing to "continue", so the topic
        # is trivially new. The model should still NAME it (a real title like
        # "Pager Opsgenie"), but its "prefer is_new_topic=false when in doubt"
        # bias would otherwise reject the very first topic and produce the
        # fallback "New Wiki". Force the verdict to new; keep the model's
        # proposed title/topic/description.
        trivially_new = not existing
        user = (
            f"Existing wikis (id, title, topic):\n{items}\n\n"
            f"Message to classify:\n{text}\n\n"
            "Respond with the JSON object only."
        )
        try:
            raw = self.complete(self._TOPIC_SYSTEM, user, json_mode=True,
                                max_tokens=600)
            data = extract_json(raw)
        except LLMNotConfigured:
            raise
        except Exception as e:
            logger.warning("topic evaluation failed (%s); defaulting to continue", e)
            # Safe default: keep using what the deterministic router found.
            return {"is_new_topic": False, "belongs_to": None,
                    "topic": "", "title": None, "description": "",
                    "reason": "topic evaluation unavailable"}

        return {
            # When there is no existing wiki, this is trivially a new topic
            # regardless of the model's doubt.
            "is_new_topic": trivially_new or bool(data.get("is_new_topic")),
            "belongs_to": data.get("belongs_to") or None,
            "topic": (data.get("topic") or "").strip()[:120],
            "title": data.get("title") or None,
            "description": (data.get("description") or "").strip(),
            "reason": (data.get("reason") or "").strip(),
        }
