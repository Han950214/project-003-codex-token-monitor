# Telemetry Model

The MVP model is a local JSON-compatible schema. Field names stay English for stable integration.

## AgentRun

- `run_id`
- `session_id`
- `title`
- `started_at`
- `ended_at`
- `elapsed_seconds`
- `steps`
- `prompt_artifact`
- `output_artifact`
- `token_estimate`
- `repo_before`
- `repo_after`
- `waste_signals`
- `cache_risks`
- `recommendations`

## AgentStep

- `step_id`
- `kind`
- `summary`
- `started_at`
- `ended_at`
- `tokens`

## PromptArtifact

- `source`
- `text`
- `sha256`
- `approx_chars`
- `estimated_tokens`

## OutputArtifact

- `source`
- `text`
- `sha256`
- `approx_chars`
- `estimated_tokens`

## TokenEstimate

- `input_tokens`
- `output_tokens`
- `optional_log_tokens`
- `stable_prefix_tokens`
- `observed_cached_input_tokens`
- `cached_input_tokens`
- `uncached_input_tokens`
- `current_tokens`
- `current_hit`
- `current_cost`
- `source`

`source` is `local_estimate` unless real provider usage is explicitly available.

## TokenSnapshot

- `session_id`
- `round_index`
- `current_run_tokens`
- `session_tokens`
- `current_cache_hit_estimate`
- `average_cache_hit_estimate`
- `current_context_tokens`
- `configured_context_window`
- `context_usage`
- `compression_threshold`
- `current_cost`
- `session_cost`
- `budget_remaining`

## RepoSnapshot

- `captured_at`
- `git_root`
- `branch`
- `status_short`
- `changed_files`
- `diff_stat`

## WasteSignal

- `kind`
- `severity`
- `evidence`
- `estimated_wasted_tokens`

## CacheRisk

- `kind`
- `severity`
- `evidence`
- `recommendation`

## OptimizationRecommendation

- `kind`
- `summary`
- `suggested_prompt`
- `estimated_savings_tokens`

## PricingConfig

- `model`
- `input_token_price`
- `cached_input_token_price`
- `output_token_price`
- `unit_tokens`
- `currency`

## BudgetState

- `configured_budget`
- `session_cost`
- `budget_remaining`
- `currency`
- `source`

All billing-like fields are 本地估算 / local estimate unless a future integration stores explicit observed usage.

