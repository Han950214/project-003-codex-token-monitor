# Codex Token Waste Report

> 所有 token、cache hit、cost、budget、context usage 均为本地估算 / local estimate，不代表真实账单、真实余额或 provider 官方 usage。

## Run Summary

- Run: `{{run_id}}`
- Session: `{{session_id}}`
- Started: `{{started_at}}`
- Ended: `{{ended_at}}`
- Duration: `{{elapsed_seconds}}s`

## Token Telemetry

- Prompt tokens: `{{input_tokens}}` 本地估算 / local estimate
- Output tokens: `{{output_tokens}}` 本地估算 / local estimate
- Current run tokens: `{{current_tokens}}` 本地估算 / local estimate
- Current cache hit: `{{current_hit}}` 本地估算 / local estimate
- Average cache hit: `{{average_hit}}` 本地估算 / local estimate
- Current cost: `{{current_cost}}` 本地估算 / local estimate
- Session cost: `{{session_cost}}` 本地估算 / local estimate
- Budget remaining: `{{budget_remaining}}` 本地估算 / local estimate

## Repo Snapshot

- Branch: `{{branch}}`
- Changed files: `{{changed_files}}`
- Diff stat: `{{diff_stat}}`

## Waste Signals

`{{waste_signals}}`

## Cache Hit Advisor

`{{cache_risks}}`

## Next Low-Token Prompt

```text
{{suggested_prompt}}
```

