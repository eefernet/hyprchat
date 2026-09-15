"""Per-search budget and diagnostics shared by all interactive search callers.

ContextVar keeps concurrent chats isolated without changing the list-returning
provider API used by deep research. No network calls or process-global budget.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchRun:
    deadline: float
    context_budget: int
    plan: Any = None
    raw: list = field(default_factory=list)
    pages: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    tasks: set = field(default_factory=set)
    fallback_state: dict = field(default_factory=lambda: {"remaining": 1})
    partial: bool = False
    started: float = field(default_factory=lambda: asyncio.get_running_loop().time())

    def remaining(self, reserve=0):
        return max(0.0, self.deadline - asyncio.get_running_loop().time() - reserve)

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        return task

    async def close(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


current_run: ContextVar[SearchRun | None] = ContextVar("web_search_run", default=None)


def classify_failure(value: str) -> str:
    low = value.lower()
    if any(s in low for s in ("429", "too many requests", "captcha", "rate limit")):
        return "rate_limited"
    if "proxy" in low:
        return "proxy_error"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if any(s in low for s in ("connect", "network", "dns", "resolve", "unreachable")):
        return "connection_error"
    if "suspend" in low:
        return "suspended"
    return "engine_error"


def diagnose_response(status_code: int, data=None) -> dict:
    """A healthy HTTP listener does not imply working upstream engines."""
    if status_code >= 400:
        return {"status": "rate_limited" if status_code == 429 else "http_error",
                "http_status": status_code, "result_count": 0, "engines": []}
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return {"status": "invalid_response", "result_count": 0, "engines": []}
    engines = []
    for item in data.get("unresponsive_engines", []) or []:
        name, error = (str(item[0]), str(item[1])) if isinstance(item, (tuple, list)) and len(item) > 1 else (str(item), "unknown error")
        engines.append({"engine": name[:100], "error": error[:200], "kind": classify_failure(error)})
    count = sum(isinstance(item, dict) and bool(item.get("url")) for item in data["results"])
    status = "partial" if count and engines else "ok" if count else "no_results"
    if not count and engines:
        kinds = {e["kind"] for e in engines}
        status = next((k for k in ("connection_error", "proxy_error", "timeout", "rate_limited", "suspended") if k in kinds), "engine_error")
    return {"status": status, "result_count": count, "engines": engines}


FAILURE_MESSAGES = {
    "connection_error": "Search engines could not connect to the internet",
    "proxy_error": "The web proxy could not connect",
    "timeout": "Search reached its time limit",
    "rate_limited": "Search engines are rate limiting requests",
    "http_error": "The search service returned an HTTP error",
    "invalid_response": "The search service returned an invalid response",
    "suspended": "Search engines are temporarily suspended",
    "engine_error": "Search engines returned errors",
    "no_results": "No matching sources found",
}
