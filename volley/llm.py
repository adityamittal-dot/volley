"""Claude calls with per-task model routing and a hard daily spend cap.

Haiku does the reading (analyst, fixer); Sonnet writes the email. Every call
records its token usage in data/usage.json and refuses to run once the day's
budget is spent, so a bad loop can't drain the credit.
"""
import anthropic

from . import store

# $ per 1M tokens: input, output. Cache reads bill at 10% of input, writes at 125%.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}


class BudgetExceeded(Exception):
    pass


_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3)
    return _client


def _cost(model, u):
    pin, pout = PRICES.get(model, (5.00, 25.00))
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    return (u.input_tokens * pin + cache_read * pin * 0.1 + cache_write * pin * 1.25
            + u.output_tokens * pout) / 1_000_000


def spent_today():
    return store.load_usage().get(store.iso()[:10], {}).get("usd", 0.0)


def _record(model, task, response):
    usage = store.load_usage()
    day = usage.setdefault(store.iso()[:10], {"usd": 0.0, "calls": {}})
    usd = _cost(model, response.usage)
    day["usd"] = round(day["usd"] + usd, 5)
    day["calls"][task] = day["calls"].get(task, 0) + 1
    store.save_usage(usage)
    return usd


def structured(task, model, system, user, schema, budget, max_tokens=2000):
    """One structured-output call. `system` is cached (it's identical across leads)."""
    if spent_today() >= budget:
        raise BudgetExceeded(f"daily budget ${budget:.2f} reached")
    kwargs = {}
    if model != "claude-haiku-4-5":
        # Short, well-specified writing: thinking adds cost without improving a 90-word email.
        kwargs["thinking"] = {"type": "disabled"}
    response = client().messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        **kwargs,
    )
    _record(model, task, response)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ValueError(f"{task}: no parsed output (stop_reason={response.stop_reason})")
    return response.parsed_output
