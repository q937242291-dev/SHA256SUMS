"""Model API clients configured through process environment variables."""
import json
import os
import urllib.request
from .privacy import redact_text


class LLM:
    def __init__(self, provider="compatible", label=None, max_tokens=4096, temperature=0):
        self.provider = provider
        self.key = os.environ.get("PTRACE_API_KEY")
        self.base_url = os.environ.get("PTRACE_BASE_URL", "").rstrip("/")
        self.model = os.environ.get("PTRACE_MODEL")
        self.label = label or self.model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.tokens = 0
        self.tokens_observed = True
        if not self.key or not self.base_url or not self.model:
            raise ValueError("Set PTRACE_API_KEY, PTRACE_BASE_URL and PTRACE_MODEL in the process environment")
        if not self.base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("API base URL must use HTTPS or a local endpoint")

    def complete(self, system, user):
        if self.provider == "anthropic":
            url = self.base_url + "/messages"
            headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
            payload = {"model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature,
                       "system": system, "messages": [{"role": "user", "content": user}]}
        else:
            url = self.base_url + "/chat/completions"
            headers = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
            payload = {"model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                obj = json.load(response)
            if self.provider == "anthropic":
                text = "".join(c.get("text", "") for c in obj["content"] if c.get("type") == "text")
                usage = obj.get("usage", {})
                count = usage.get("input_tokens", 0) + usage.get("output_tokens", 0) if "input_tokens" in usage and "output_tokens" in usage else None
            else:
                text = obj["choices"][0]["message"]["content"]
                count = obj.get("usage", {}).get("total_tokens")
            if count is None:
                self.tokens_observed = False
            else:
                self.tokens += count
            return text
        except Exception:
            # Return the request failure through the client error interface.
            raise RuntimeError("Model request failed; check local provider settings") from None

    def json(self, system, user):
        text = self.complete(system, user).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            result = json.loads(text)
        except (TypeError, ValueError):
            raise ValueError("Model returned invalid JSON") from None
        if not isinstance(result, dict):
            raise ValueError("Model response must be a JSON object")
        return result
