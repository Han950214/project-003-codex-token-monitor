# Cache Hit Advisor

Cache Hit Advisor estimates whether the next Codex prompt is likely to preserve a stable prefix and avoid avoidable repeated context.

## Signals

- Stable prefix tokens: estimated tokens before the first highly dynamic block.
- Repeated background: large text repeated across multiple runs.
- Dynamic prefix risk: timestamps, sorted file lists, fresh logs, generated summaries, or changing status text before stable instructions.
- Ordering risk: same items appearing in changing order between runs.
- Instruction drift: user repeats old constraints plus new constraints in conflicting ways.

## Recommendations

- Put stable project rules and durable context first.
- Put timestamps, current git status, fresh logs, and run-specific details later.
- Replace repeated background with a short reference when the assistant already has context.
- Ask for an incremental change instead of replaying a long prior plan.
- Keep low-token instructions explicit: `Use low token mode; only inspect files needed for this task.`

## Disclaimer

Cache hit values are 本地估算 / local estimate. The advisor cannot guarantee Codex Desktop or provider cache behavior.

