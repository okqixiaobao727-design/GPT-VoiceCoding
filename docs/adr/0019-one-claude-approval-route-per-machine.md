# 19. One Claude approval route per user per machine, and the first live engine holds it

Date: 2026-09-02 · Status: Accepted · Source: [#202](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/202)

The `PermissionRequest` hook is a process Claude Code starts, once per displayed dialog, with no configuration and no parent in this engine (ADR 0011). The one thing it can read is the address the engine published at a fixed path, `~/Library/Application Support/GPT-VoiceCoding/engine/address.json` (`locations.py:56`). That path is derived from the user's home directory, so it is one file per user per machine — while an engine is not.

Two engines do run on one machine: two acceptance lanes at once, or an acceptance engine beside the installed app. Publishing and withdrawing were both unconditional, so the last engine to start owned every Claude permission dialog on the machine and the first to stop took the address away from the one still up. Both engines also wrote through one fixed temporary name beside the address: in acceptance run `20260902T012313Z`, in the same millisecond, `engine-codex` logged `approval address published` and `engine-claude` logged `[Errno 2] No such file or directory: '….address.json.writing' -> '…/address.json'`.

## Decision

**The published address is a claim, and only one engine per user per machine holds it.**

Publishing dials whatever address is already there. A socket nobody answers is debris and is taken over. A socket that answers and is not this engine's own means another engine holds the route: this engine does **not** overwrite it. It logs a warning naming the holder's socket, runs without the Claude Approval Relay for this run — the same degraded start the adapter already takes when its own approval socket will not bind — and otherwise starts normally.

**Withdrawal removes only the engine's own address.** On close the adapter reads the file and unlinks it only if it still names this engine's socket. Another engine's address, and a file this engine cannot attribute, are both left alone.

**The temporary file has a name of its own per write**, so two publishers cannot collide on it.

**Deciding and acting are one step.** Probing the incumbent and writing are two syscalls, and so are reading the owner and unlinking. Engines interleaved between the halves of either pair put the file back where this issue found it: publishers that all see no holder all write, and an engine withdrawing after a newer one took over would unlink an address that is no longer its own. Both pairs are taken under one `flock` beside the address. It is an advisory lock rather than an exclusive create, because a lock has to survive the process holding it being killed — the kernel drops an `flock` when the descriptor closes, while an `O_EXCL` marker left by a killed engine is a machine that can never publish again.

**An engine that published no address says so where the engine reports what it loaded** (ADR 0003), naming the reason — the holder, when the reason is a peer — in addition to the log line, and the report is honest about how far the failure reaches. Both Claude routes read this one address — the `PermissionRequest` hook (`approval_hook.py`) and the `SessionStart` registration hook (`registration.py`) — so an engine that published no address loses more than approvals: no Session can register with it either, and its roster stays empty for that reason. `verify` therefore has two branches. An unpublished address with an empty roster is **FAIL**, whatever the cause — refused by a peer, a socket that would not bind, an address file that could not be written ([#204](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/204)) — naming the reason and saying why the roster is empty; the empty-roster branch would otherwise report PASS — "no Claude Session is registered, so there is no inbox to reach" — which is precisely the guard that says nothing while the route is dead that ADR 0003 exists to prevent. That substitution is the **only** one: every other answer the report can give is a reason of its own — a missing hook block, a registry outside the config directory, an inbox that stopped answering — and each keeps its own outcome and its own detail, with the refusal prefixed rather than put in its place. A report that hid a missing hook block behind the refusal would be ADR 0003's original failure wearing this issue's clothes.

There is no Telegram or menu-bar surface, because only development and acceptance ever run two engines.

**The hook is unchanged**, and the route stays one file. Claude Code's user-scope hooks block and its session roster are per-user singletons; the address follows them.

## Consequences

The rule is **ported** from legacy `bridge/daemon.py:711` (`_claim_socket_path`, reference state `1d32845`): "take over a stale socket file, but never displace a live Bridge." Legacy applied it to its control socket and never needed it for an address, because it injected the address into every Session it launched itself (`bridge/claude.py:468`) — which ADR 0011 adapted away when v1.0 became a bridge over Sessions the user starts. The own-address check on withdrawal has no legacy equivalent: one bridge per socket path made the comparison moot. It is **dropped there, needed here**.

Which engine wins is now decided by who starts first, which is a clock. Where that is not good enough, it is decided by configuration instead: the acceptance harness drops the Claude agent adapter from the Codex lane's derived config (`tests/acceptance/support.py`, `derive_config`), leaving exactly one claimant when both lanes are up.

A developer running the app while an acceptance engine holds the address reaches no Claude Session at all, and hears it from a warning and a red `verify` naming the holder, rather than from a silently rerouted dialog. That is the trade this accepts: a named failure over an invisible one. Making the second engine *useful* rather than merely honest would mean giving the hook a way to find an engine other than this one file, which this decision deliberately does not do — the route stays one file, because Claude Code's user-scope hooks block and its session roster are per-user singletons.
