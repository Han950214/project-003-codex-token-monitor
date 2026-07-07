# Desktop UI Flow

The MVP Dashboard is a Windows desktop floating window built as a local mock UI.

## Primary Flow

1. User opens the Dashboard.
2. User clicks `开始监控 / Start Run`.
3. User pastes or records the current Codex prompt.
4. User performs the Codex task normally.
5. User pastes or records the Codex output.
6. User clicks `结束监控 / End Run`.
7. Dashboard updates local estimates, git summary, waste report, and advisor suggestions.

## Dashboard Regions

- Header: run name, session round, manual start/end controls.
- Prompt panel: current prompt text and estimated prompt tokens.
- Output panel: current output text and estimated output tokens.
- Report panel: token waste report, changed files, diff stat.
- Advisor panel: stable prefix estimate, repeated background warnings, dynamic-prefix risk warnings, next prompt suggestion.
- Bottom telemetry bar: compact session/current run local estimates.

## User Visible Text Rule

Dashboard / telemetry bar / report wording should use Chinese or Chinese-English pairs. Every metric must include `本地估算 / local estimate` when it could be confused with real usage.

