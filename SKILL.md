# Codex Token Monitor Skill

## When To Use

Use this skill when the user wants to inspect, estimate, or improve token usage in a Codex Desktop workflow:

- user instruction to Codex
- Codex task execution notes
- Codex output
- estimated prompt/output tokens
- estimated cache hit risk
- repeated context waste
- next-round low-token prompt recommendations

## What It Monitors

- Manual run start/end time and duration.
- Pasted or recorded prompt artifacts.
- Pasted or recorded output artifacts.
- Local token estimates for input, output, optional logs, session total, and current run total.
- Local cache hit estimates based on observed cached input tokens when available, otherwise stable prefix tokens.
- Git before/after status, changed files, and diff stat.
- Cache risks such as repeated background text, timestamps in stable prefixes, shuffled ordering, and dynamic content inserted before stable context.
- Optimization recommendations for shorter, cache-friendly prompts.

## What It Does Not Do

- It does not modify the AOS main repository.
- It does not read Codex hidden reasoning tokens.
- It does not force Codex Desktop cache hits.
- It does not claim local estimates are real bills, balances, or provider usage.
- It does not upload data to a cloud service.
- It does not implement accounts, commercial billing, keyboard listening, browser plugins, or credential access.

## Privacy And Sensitive Information

默认本地保存，不上传云端。用户粘贴到 Dashboard 或报告中的 prompt/output 可能包含敏感信息，因此报告默认只落在本项目本地目录，并应避免提交真实凭据、私密业务内容或客户数据。

All token, cache hit, cost, budget, and context usage values are 本地估算 / local estimate. If a future provider or Codex API exposes real usage, real values may override estimates only when the source is explicit.

## Low Token Working Rules

- Prefer concise prompts with stable reusable context first and task-specific changes last.
- Avoid pasting the same long background on every round when a short reference will work.
- Keep timestamps, generated file lists, and changing logs out of the stable prefix.
- Ask for incremental changes instead of resending entire prior plans.
- Treat advisor output as guidance, not as guaranteed cache behavior.

## Later AOS Integration

Future AOS integration should happen through a narrow adapter boundary:

- export local run/session telemetry as versioned JSON
- allow AOS to read reports from an explicit local path
- keep AOS-specific code outside this repo until integration is approved
- never mutate AOS state from this skill without a separate user-confirmed plan

Current phase: independent project only. Do not modify the AOS main repository.

