# Telemetry Metrics

All formulas produce 本地估算 / local estimate values.

## Current Cache Hit

```text
if observed_cached_input_tokens exists:
  current_hit = observed_cached_input_tokens / input_tokens
else:
  current_hit = stable_prefix_tokens / input_tokens
```

If `input_tokens` is `0`, return `0`.

## Average Cache Hit

```text
average_hit = weighted average of current_hit values in the session
weight = input_tokens
```

## Current Tokens

```text
current_tokens = input_tokens + output_tokens + optional_log_tokens
```

## Session Tokens

```text
session_tokens = sum(current_tokens for session runs)
```

## Cached And Uncached Input Tokens

```text
cached_input_tokens = observed_cached_input_tokens if available else stable_prefix_tokens
uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
```

## Current Cost

```text
current_cost =
  uncached_input_tokens * input_token_price
  + cached_input_tokens * cached_input_token_price
  + output_tokens * output_token_price
```

Prices are normalized per token by the metrics module.

## Session Cost

```text
session_cost = sum(current_cost for session runs)
```

## Context Usage

```text
context_usage = current_context_tokens / configured_context_window
```

## Budget Remaining

```text
budget_remaining = configured_budget - session_cost
```

## UI Label Requirement

Every displayed value must include `本地估算 / local estimate`.
