"""One HTTP client for every model call: /v1/chat/completions.

Ollama serves this at http://127.0.0.1:11434/v1 during development. A
packaged install starts llama-server, which serves the same endpoint, so the
pipeline never changes between the two. Images travel as base64 data URLs.
"""
import base64
import io
import json
import re

import requests

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


class ModelError(RuntimeError):
    pass


def is_up(base: str, timeout: float = 3.0) -> bool:
    try:
        r = requests.get(f"{base}/models", timeout=timeout)
        return r.status_code < 500
    except requests.exceptions.RequestException:
        return False


def models(base: str, timeout: float = 5.0) -> list:
    try:
        r = requests.get(f"{base}/models", timeout=timeout)
        data = r.json().get("data") or []
        return [m.get("id") for m in data if isinstance(m, dict)]
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return []


def shrink(img, max_px: int):
    """The image with its longest side at most max_px (never upscaled). A
    vision model spends context tokens per image patch, so a 300 dpi strip
    thousands of pixels tall would swallow the whole window on its own."""
    if not max_px or max(img.width, img.height) <= max_px:
        return img
    f = max_px / float(max(img.width, img.height))
    from PIL import Image
    return img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))), Image.LANCZOS)


def image_part(img, max_px: int = 0) -> dict:
    """A PIL image as an OpenAI-style image_url content part."""
    buf = io.BytesIO()
    shrink(img, max_px).convert("RGB").save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def chat(base: str, model: str, messages: list, schema: dict | None = None, temperature: float = 0.0,
         timeout: float = 600.0, max_tokens: int = 2048) -> str:
    """The assistant's text. With `schema`, structured output is requested as
    a JSON schema; a server that only knows json_object gets that instead."""
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            "stream": False}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": schema.get("title", "answer"), "schema": schema}}
    text = _post(base, body, timeout)
    if text is None and schema is not None:
        body["response_format"] = {"type": "json_object"}
        text = _post(base, body, timeout)
    if text is None:
        raise ModelError(f"{model} at {base} returned no usable reply")
    return text


def _post(base: str, body: dict, timeout: float):
    try:
        r = requests.post(f"{base}/chat/completions", json=body, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise ModelError(f"no model server at {base} (is Ollama running? `ollama serve`)")
    except requests.exceptions.RequestException as e:
        raise ModelError(f"model request failed: {e}")
    if r.status_code == 400 and body.get("response_format", {}).get("type") == "json_schema":
        return None                      # the server does not take a schema; caller retries
    if r.status_code == 404:
        raise ModelError(f"model {body['model']!r} is not available at {base} "
                         f"(pull it: `ollama pull {body['model']}`)")
    if r.status_code >= 400:
        if "context size" in r.text or "exceed_context" in r.text:
            raise ModelError("the request did not fit the model's context window; for Ollama set the user "
                             "environment variable OLLAMA_CONTEXT_LENGTH=16384 and restart it "
                             f"(server said: {r.text[:200]})")
        raise ModelError(f"model server answered HTTP {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError(f"unreadable reply from the model server: {r.text[:300]}")
    if not str(content or "").strip() and (msg.get("reasoning") or msg.get("reasoning_content")):
        # a "thinking" model spent the whole output budget reasoning and never answered
        raise ModelError(f"{body['model']} returned only reasoning and no answer; use a non-thinking build "
                         f"(for Ollama: the -instruct tag, e.g. qwen3-vl:4b-instruct)")
    return content


def parse_json(text: str):
    """The first JSON object in a reply, fences and chatter stripped."""
    if text is None:
        raise ValueError("empty reply")
    s = _FENCE.sub("", str(text)).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unterminated JSON object in reply")
