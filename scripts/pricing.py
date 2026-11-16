"""Official standard GPT-6 Luna pricing shared by reporting and the proxy."""

PRICING = {
    "model": "gpt-6-luna", "currency": "USD", "checked_date": "2026-11-16",
    "service_tier": "Standard (proxy forces service_tier=default)",
    "rates_per_million_tokens": {
        "fresh_input": 0.10, "cache_read": 0.01,
        "cache_write_30m": 0.125, "cache_write_12h": 0.25, "output": 0.50,
    },
    "long_context": {"input_tokens_strictly_greater_than": 272000,
                     "input_and_cache_multiplier": 2.0, "output_multiplier": 1.5,
                     "scope": "All tokens in the request, not only the excess."},
    "formula": "((input-read-write)*fresh_rate + read*read_rate + write30m*write30m_rate + write12h*write12h_rate)*input_multiplier/1e6 + output*output_rate*output_multiplier/1e6",
    "scope": "Standard text-token list-price estimate without regional premiums or account-specific discounts; not an invoice.",
    "sources": [
        {"url": "https://developers.openai.com/api/docs/models/gpt-6-luna",
         "supports": "Luna standard rates and full-request long-context multipliers."},
        {"url": "https://developers.openai.com/api/docs/guides/prompt-caching",
         "supports": "30m writes cost 1.25x fresh input, 12h writes cost 2.5x; writes are total rates, read/write counts do not overlap, and TTL write components sum to all writes."},
    ],
}


def estimate_cost(entry):
    if not entry.get("usage_complete") or not entry.get("usage"):
        return None
    usage = entry["usage"]
    details = usage.get("input_tokens_details", {})
    tokens_in, tokens_out = usage["input_tokens"], usage["output_tokens"]
    reads, writes = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    fresh = tokens_in - reads - writes
    if min(tokens_in, tokens_out, reads, writes, fresh) < 0:
        raise ValueError(f"Invalid input/read/write token partition: {entry['run_id']} call {entry.get('call')}")
    ttl = details.get("cache_write_tokens_details", {})
    if writes and not ttl:
        return None
    write30m, write12h = ttl.get("ttl_30m_tokens", 0), ttl.get("ttl_12h_tokens", 0)
    if min(write30m, write12h) < 0 or write30m + write12h != writes:
        raise ValueError(f"Cache-write TTL counts do not sum to writes: {entry['run_id']} call {entry.get('call')}")
    rates = PRICING["rates_per_million_tokens"]
    long_context = tokens_in > PRICING["long_context"]["input_tokens_strictly_greater_than"]
    input_multiplier = PRICING["long_context"]["input_and_cache_multiplier"] if long_context else 1
    output_multiplier = PRICING["long_context"]["output_multiplier"] if long_context else 1
    input_cost = (fresh * rates["fresh_input"] + reads * rates["cache_read"] +
                  write30m * rates["cache_write_30m"] + write12h * rates["cache_write_12h"])
    return (input_cost * input_multiplier + tokens_out * rates["output"] * output_multiplier) / 1_000_000
