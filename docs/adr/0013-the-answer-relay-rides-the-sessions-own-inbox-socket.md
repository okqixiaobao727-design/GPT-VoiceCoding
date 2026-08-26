# 13. The Claude Answer Relay rides the Session's own inbox socket, and an accepted write is not a receipt

Date: 2026-08-26 · Status: Accepted · Source: [#71](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/71), built in [#77](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/77)

v1.0 is a bridge over the Sessions the user already started ([#67](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/67)), and the route ADR 0006 chose cannot reach one: a channel server is spawned from a plugin manifest at launch, so it exists only in Sessions this product started. Claude Code has since grown an inbox socket of its own, bound by default for every Session. The documented external ingress routes are channels — a research preview requiring a startup `--channels` selector, which means taking over the user's `claude` command — and `-p --input-format stream-json`, which is not an interactive Session at all. Neither reaches a Session started by hand.

## Decision

**The Answer Relay writes into the Session's own inbox socket, on private surface, knowingly** (Simon, 2026-08-25). Four things follow, and they are the decision rather than its background.

**1. The socket path is read, never built.** 2.1.245 derives the socket directory from `CLAUDE_CODE_TMPDIR` or `$XDG_RUNTIME_DIR` and accepts `--messaging-socket-path`, so a constructed path is a guess. It arrives on the `SessionStart` registration (ADR 0011) and is held per exact target.

**2. `DELIVERED` has exactly two sources, and an accepted write is neither.** The line was taken by a socket, not read by a Session. The two are the `held → delivered` `peer_message_status` receipt, correlated by `orig_msg_id`; and the target's own transcript entry, whose `origin.from` is our reply address and whose `origin.msg_id` is the id we minted. `denied` / `refused` / `expired` / `dropped` are proven non-delivery and grade `FAILED`; `held` is its own grade; everything weaker is `UNKNOWN`, which P9 never re-sends on this system's own authority.

**3. Words travel on this wire; authority never does.** A peer message is announced to the receiving Session as not typed by its user, and upstream enforces it — a Session asked to approve a pending dialog on a peer's say-so refuses of its own accord and names it permission laundering. **So a Session's question can never be answered over this route**, and no later work on the socket can recover that half. It is why the `PermissionRequest` hook route (ADR 0011) exists rather than being an optimisation.

**4. To be receipted at all, this engine publishes a peer key.** A receiver answers only a sender it can resolve, which means a reply socket bound inside the receiver's own socket namespace and a `<pid>.<sha256(socket path)>.key` file published beside the Session records. It is *only* a key — no `<pid>.json` — so nothing this engine does puts a phantom row in a Session roster. One reply socket per socket directory, because the directory is the namespace a receipt may be addressed inside. Both the socket and the key live in directories that are not ours, and both are removed when the engine stops.

**No version pin** (Simon, 2026-08-25): Claude Code auto-updates, and when it breaks we fix it then. The safeguard is not a pin but the rule the evidence already forces — never infer delivery from a successful write — so an upstream change surfaces as a missing receipt rather than as words the user believes were delivered. The frame shapes are not restated here: they live in `adapters/agent/claude/inbox.py`, beside the code that sends them, because they are what a re-probe compares against.

## Consequences

Every Claude Session on the machine is reachable, including ones started before this engine was running, and the reference implementation's launch wrapper is not needed for the Relay half.

**Most `UNKNOWN`s on this lane are Relays that did arrive.** An immediately-accepted message yields no receipt to an external process — the receiver logs `[peer-cred] peer pid unavailable`, and its notion of verification is *own-child*, a process posting to its parent's socket. On a machine with `crossSessionInbound: "accept"` nothing is ever held and therefore nothing is ever receipted, so the transcript is the whole of the evidence there. That is the price of the honesty rule, paid deliberately.

**A grade that has not finished happening keeps a listener, and both directions are raised.** A `held` message settles when the person answers, expires after about five minutes, or is dropped once the hold queue passes a hundred. So `HELD` and `UNKNOWN` keep a watcher for that lifetime: a later `delivered` is raised so the hub stops holding words that arrived, and a later `denied` / `expired` / `dropped` is raised as `FAILED`, which is the one grade P9 permits another attempt for. Without the second half a Relay would stay recorded as parked long after it was thrown away, which is the implied delivery this ADR exists to refuse. A grade that was already terminal never gets a listener, so nothing here re-grades a settled attempt.

**The auth frame carries the receiver's own messaging token** when its registration reported one, and is omitted otherwise. That token's one documented meaning is own-child verification, and it is the documented way past a `bypassPermissions` receiver's hold; delivery does not need it, and neither it nor our own token earns a receipt. Attesting `from_mode` was hypothesised and disproven — an external process cannot assert a permission class — so this product does not send one.

**This is undocumented surface and the acceptance is where it is checked.** The `relay` step exercises the whole route against a real Session; a build that changed the frame, the key derivation or the `procStart` shape shows up there as a lost receipt.

ADR 0006 is superseded. ADR 0007 was already superseded by ADR 0011, which is the other half of this map: the inbox for words, the hooks for authority.
