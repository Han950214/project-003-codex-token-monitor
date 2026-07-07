# Product Brief

Codex Token Monitor Skill is a local Windows desktop monitoring aid for Codex Desktop workflows. The first version focuses on manual capture, local estimates, visual feedback, and report generation.

## Product Goal

Help the user understand:

- what instruction was sent to Codex
- what Codex produced
- how many prompt/output tokens were likely used
- where context repetition may be wasting tokens
- which prompt changes may improve cache friendliness next round

## MVP Boundaries

第一版是桌面浮窗 / Dashboard 优先，不是 CLI-only。CLI or wrapper collection can be added later as an optional backend, but the primary user experience is visual.

All token, cache hit, cost, budget, and context usage values are 本地估算 / local estimate. They must never be described as real bills, real balances, or guaranteed provider usage.

## Out Of Scope

- AOS integration
- cloud sync
- account system
- keyboard listener
- browser plugin
- real credential reading
- hidden reasoning token extraction
- commercial billing
- push/release automation

## Success Criteria

- User can manually start/end a monitored run.
- User can paste prompt and output.
- Dashboard shows current and session telemetry.
- Cache Hit Advisor explains repeated context and cache risks.
- Local report template can summarize token waste and next prompt suggestions.
- The project remains independent and local-first.

