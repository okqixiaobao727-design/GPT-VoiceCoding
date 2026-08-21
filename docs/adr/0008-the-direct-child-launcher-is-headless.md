# 8. The direct-child launcher is headless, and visibility is the tmux adapter's job

Date: 2026-08-21

Status: Accepted

Taken in: [Build: Session Launcher adapters — direct child and tmux](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/9)

## Context

The decoupling verdict took tmux off the control path and left the Session
Launcher with two adapters: a direct child process, which is the default and
needs nothing installed, and an optional tmux adapter which degrades to "the
human launches it themselves" when tmux is absent.

That leaves one question the verdict did not answer, and everything else in the
seam turns out to hang off it: **a terminal coding agent is a TUI, and the engine
is a daemon with no terminal to offer it. So what, exactly, is a "direct child"?**

Three shapes were possible.

- **Headless on a pseudo-terminal the engine allocates and holds.** The Session
  is real and running on a real tty; nobody is watching it.
- **No terminal at all**, relying on the two agents behaving acceptably when
  their stdio is a pipe. Undocumented and unmeasured for both products.
- **A pseudo-terminal whose master end is exposed** so something could attach.
  This is a second, worse tmux, built by us, and the verdict already ruled out
  buying window management.

## Decision

**The direct-child adapter is headless.** It allocates the pseudo-terminal, keeps
the master end, and drains it. Visibility is not a property it offers.

**Visibility is the tmux adapter's single job.** It is what the optional adapter
buys, and it is the only thing that differs between the two: same argv, same
environment, same readback, same refusals.

Three consequences follow along that same line, and are part of this decision
rather than separate ones.

**1. Whoever owns the terminal owns the Codex app-server.** A Codex TUI is a thin
client of an app-server, so a launch is two processes and the question is which
of them outlives an engine restart.

- Under tmux, the app-server is started **in a tmux window**, so it belongs to
  the tmux server. A visible Session is one a human is using and an engine
  restart must not take it down. This keeps the ownership shape the Codex Agent
  adapter was built on — the terminal owns it, the engine never does — with the
  tmux server as the terminal. It also keeps the process *findable*: an orphan in
  a window list is one somebody can clean up, which a `setsid` orphan in the
  process table is not.
- Under the direct-child adapter, the app-server is an ordinary child and dies
  with the engine. This is forced rather than chosen: the TUI's pseudo-terminal
  is the engine's, so an engine that goes takes the TUI with it regardless, and
  an app-server left serving a client that no longer exists — and that nobody
  could have reached anyway — is not a Session anyone can still use.

This is an evidence-based amendment to the Codex Agent adapter's rule that the
engine *never* owns a Session's app-server. That rule's reason is preserved
exactly where it applies, and the module that states it has been corrected rather
than left to become quietly false.

**2. The child's environment is built by allowlist, and the engine's own is not a
baseline.** A tmux pane is forked by a tmux server that may have been started days
ago from a shell this engine never saw, so ADR 0004's obligation to keep
inherited noise out of a child cannot be discharged by the engine cleaning its own
environment once. Both adapters therefore state the child's environment in full:
`env -i` for the tmux pane, an explicit mapping for the direct child.

It is an allowlist rather than a filtered copy, for two reasons. ADR 0004's
variable — `MallocStackLogging`, 98.1% of a 68 MB log — was set by nobody in the
repository and inherited from the installing shell, so a subtractive rule would
only have caught it if somebody had known to name it in advance, which nobody
did. And the obvious baseline, "the engine's own environment", is where this
system keeps its *own* secrets: the Companion Channel reads its bot token from a
variable named there, and forwarding it wholesale would hand every launched
coding agent the credentials of the bridge that launched it.

**3. ADR 0004's second obligation does not apply to a tmux child, and this is a
ruling.** That obligation — give a spawned child a pipe rather than a descriptor
on the engine's log — exists because a child *the engine forks* inherits the
engine's redirected stdout and can never be told to reopen it. A tmux pane is not
forked by the engine and never had such a descriptor, so the cause is absent
rather than merely mitigated. Pane output belongs to the tmux server's own
buffer, which is the adapter's and is never the engine's log. The direct-child
adapter discharges the obligation structurally: the child's stdout and stderr are
the pseudo-terminal.

## Consequences

**Owning the master end is an obligation, not a convenience.** Nothing reads a
pseudo-terminal nobody is attached to, and the kernel blocks the writer when its
buffer fills. A TUI redraws constantly, so an undrained master is not a slow leak
— it is a Session that stops, invisibly, within seconds, in a system whose whole
purpose is noticing when a Session needs its human. The direct-child adapter
drains continuously and keeps a bounded tail; that tail is a ring buffer in
memory, not a log, and ADR 0004's rule that the engine owns exactly one log and
enumerates no adapter's is untouched.

**A Codex workspace that codex has never seen needs a visible session once.**
Measured on codex 0.149.0: a TUI shows a blocking directory-trust dialog for any
directory it has not seen, that trust is recorded per *exact* directory and is not
inherited from a parent, and a Session sitting on that dialog announces no thread
at all. A headless Session cannot answer it. So the first launch into an unknown
workspace fails, truthfully and with a message that says so and says what to do.
The launcher does **not** pass a flag to pre-trust the directory: trust decides
whether project-local config, hooks and exec policies load, and a spoken "open a
session in X" is not evidence that the speaker knows what is in X. The tmux
adapter needs no special case here — a human can see the dialog and answer it.

That cost was reopened as a product question during this build, and the answer
came back unchanged — so this paragraph stands as written rather than by default.
The convenience route was ruled out by measurement rather than by preference: `-c
projects."<path>".trust_level` reaches the trust gate from neither the TUI nor the
app-server, in neither path spelling; and, decisively, setting an *already
trusted* directory to `untrusted` the same way produces no dialog either.

A later probe, recorded on [issue
#18](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/18), narrowed
what those experiments actually measure. This paragraph first read them as "a
per-invocation trust override does not exist" and "codex reads trust from the
persisted configuration file, not from the merged configuration". The accurate
statement is *file versus override*: codex resolves trust from persisted
configuration **layers** — the base user config and any file named by `--profile`
— and ignores `-c`. A `--profile` layer does reach the gate, held down by an
inverted control: the same flag and the same engine-owned file, differing only in
whether the workspace is listed, shows the dialog or does not. That is no route
for *this* launcher — `codex app-server` has no `--profile`, and a remote TUI
resolves trust by asking the app-server rather than from its own layers — but it
makes pre-trusting a question of **which** file gets written rather than an
impossibility. Every route that fits this topology writes a file another program
owns, which is why the sanctioned route is issue #18's to take and not this
decision's.

**This does not apply to Claude Code.** Measured on 2.1.238 by the same method — a
real tty, an unfamiliar directory, no keystrokes sent — a Claude Session starts
straight to its prompt with no trust dialog and registers itself immediately.
That is a *measured absence* rather than an untested assumption, and it is
recorded as such so that a future change can be noticed rather than rediscovered.

**Nothing pre-approving is passed on either side.** Claude Sessions are launched
with `--permission-mode default`; Codex threads are started with no
`approvalPolicy`. Whatever a launch waves through, the Approval Relay never sees,
so the launcher chooses how much of that capability exists. It is stated rather
than inherited, because a product default that moved would silently move this.

**The engine's shutdown means opposite things to the two adapters.** The
direct-child adapter closes every Session it holds: they are unreachable once it
is gone. The tmux adapter closes none: a human's window is not this engine's to
close, and a restarted engine attaches to what is still there.
