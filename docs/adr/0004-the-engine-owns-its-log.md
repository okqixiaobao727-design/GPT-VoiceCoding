# 4. The engine owns its log, so rotation can rename rather than truncate

Date: 2026-08-18 · Status: Accepted · Source: [legacy ADR 0004](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0004-bounded-log-files.md) (measurement), amended in [#4](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/4) and [#33](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/33)

The legacy log grew ~1 GB/month, 98% one inherited-environment `libmalloc` line, and could not be rotated because a shell redirect owned the descriptor. Copy-truncate loses lines written between the copy and the truncate.

## Decision

- **The engine owns its log**: it opens the configured file and `dup2`s it onto its own stdout/stderr before the engine object exists; nothing that starts the engine redirects output.
- **Rotation is rename-and-reopen**, so no write can be dropped. **The cap binds every generation**; rotation keeps the newest bytes.
- **Noise is stripped at the environment**: variables matching configured prefixes are removed once by every process that spawns others.
- **Max bytes, retained generations and stripped prefixes have no compiled-in default** — they are measured decisions. The log path defaults beside the state file, being a location rather than a decision.

## Consequences

A log the engine cannot tell to reopen (held by a third-party child) falls back to truncate-in-place and must never be `bridge.logFile`. A child already running at rotation keeps writing into the renamed generation, so a generation is trimmed **on its own inode**, never replaced; zero retention empties the live file in place rather than unlinking it. Of exact ceiling / no severed inode / no dropped write, this takes the first two; the residual is one raw write inside a generation's in-place trim. Output before adoption is not logged, but is inherited by the shell and reaches the Retry panel, in order and complete, before that run's exit is observable.
