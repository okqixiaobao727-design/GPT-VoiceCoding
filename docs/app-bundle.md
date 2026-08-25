# The app bundle

One command, from a clean checkout, to a signed `GPT-VoiceCoding.app`:

```bash
scripts/build-app.sh
```

That is the whole interface. The pipeline itself is [`app_bundle/`](../app_bundle),
in Python, under the project's own test suite; the script is one line that runs
it. Three other things it will do:

```bash
scripts/build-app.sh --debug            # a debug build of the shell
scripts/build-app.sh --without-engine   # the shell alone, for the developer loop
scripts/build-app.sh lock               # regenerate the dependency lock
```

`--without-engine` is the successor to the old `dev-app.sh`. It builds a bundle
the shell's own resolver falls through past, to `GPTVOICECODING_ENGINE_PYTHON` or
`PATH` — the developer path, which is a stated feature rather than a degraded
build.

After the engine is installed, the pipeline inspects every ordinary executable
text file in `engine/bin/`. A Python console script whose shebang names the
interpreter in this build tree is given a shell/Python preamble that re-executes
the `python3` beside the script; shell scripts and binaries are left as they are.
There is no list of console-script names, so a script added by a future locked
dependency takes the same path automatically. Before signing, the pipeline also
checks every text file in the assembled `.app` and refuses the build if any one
still names the source checkout. The real bundle build in CI runs that same
check.

## What ends up inside

```
GPT-VoiceCoding.app/
└── Contents/
    ├── Info.plist                    the bundle's identity, copied from shell/Resources
    ├── MacOS/GPTVoiceCodingShell     the menu-bar shell, and nothing else
    └── Resources/
        ├── config.example.toml       shipped, never installed for you
        └── engine/                   python-build-standalone, the locked wheels, the engine
            └── bin/
                ├── python3 -> python3.12    what the shell spawns
                └── bridgectl                what `[delegate] cli` names
```

The engine is inside the bundle because that is what earns the microphone grant
— see [ADR 0005](adr/0005-the-engine-lives-inside-the-app-bundle.md). Bundle
containment is the mechanism; the process tree is not.

## Why the pipeline is a plan and a doing side

`codesign --deep` is deprecated for signing, and never discovers
`Contents/Resources` anyway — so the set of things to sign is **enumerated**, not
delegated to a flag. `--deep --strict` is still the right thing to *verify* with,
and the build runs it, but it is not what finds the files.

That makes the enumeration and the signing order the two facts nothing else can
check. They are decided in `app_bundle/plan.py` and `app_bundle/signing.py`,
which read and never write, spawn or download, and they are asserted by
`tests/test_app_bundle.py`. `app_bundle/run.py` executes the result in order and
stops at the first failure.

The size of what is at stake: a bare python-build-standalone tree carries **11**
Mach-O files, and the engine with its voice extra carries **85** — the wheels are
almost all of it, and that set changes shape every time the lock is regenerated.
A missed `.so` under `lib-dynload`, or an app signed before its own contents,
produces a bundle that verifies clean and fails at the one moment it matters.

## The decisions this pipeline holds

| Decision | Why |
| --- | --- |
| python-build-standalone `install_only`, not a framework CPython | A framework CPython re-executes `Python.app/Contents/MacOS/Python` from its original location, so the process that ends up running is *outside* the bundle even though the shell spawned the one inside it. |
| Pinned by release tag **and** SHA256, and a hash-pinned lock for the wheels | A pipeline whose job is to sign a set of Mach-O files must not let an unpinned upstream change that set underneath it. Reproducibility here is a signing-integrity property, not a convenience. |
| One lock per host triple, and a refusal when there is none | A wheel's hash is architecture-specific, so one lock cannot cover two. Falling back to an unpinned install would mean signing binaries nobody reviewed. |
| Single-architecture, read from the build host | python-build-standalone publishes no `universal2` build, so a universal `.app` means lipo-ing two interpreters and two sets of wheels. Adoption-era work, which route (a) accommodates later without architectural change. |
| Ad-hoc signature, no Developer ID, no notarization | Charter decision 9: v0 targets developers. The accepted cost is that the signature changes per build, so a *new build* may re-prompt for the microphone. |
| Hardened runtime **off** | It buys nothing v0 ships — it is what notarization needs — and it is the most likely cause of the CFFI audio-callback crash the `allow-jit` escape hatch was reserved for, whose usual companion fix (`disable-library-validation`) is forbidden. It becomes the notarization-era ticket's decision, where `allow-jit` becomes live again. |
| `com.apple.security.device.audio-input` on the bundled interpreter only | Entitlements go on executables, and `python3.12` is the process that opens the device. Without the sandbox or the hardened runtime it is **inert** — belt and braces, deliberately, so the bundle is already the right shape later. It is not what earns the grant. |
| Bytecode pre-compiled at build time | Nothing may write into the bundle at runtime. The shell already sets `PYTHONDONTWRITEBYTECODE` for the bundled interpreter it spawns; this covers the two cases it cannot see — the engine run headless from a terminal, and the relocated `bridgectl` — and it also means a start does not recompile from source into memory. |
| Every Python console script is relocated, without a name list | `pip` writes an **absolute** shebang. The interpreter relocates; its scripts do not. The pipeline examines every executable text file in `engine/bin/` and replaces only a shebang that names this build's bundled interpreter. `bridgectl`, `cffi-gen-src`, `pyav`, and any future locked dependency script therefore share one mechanism. |
| The assembled bundle must not name its source checkout | A build-tree path works on the build machine and dies when that checkout or worktree is removed. The final pre-signing check scans the whole `.app`, and CI's real bundle build fails if any text file still carries the source root. Local-install provenance (`direct_url.json`) is removed because it would otherwise violate the same invariant even though the engine never reads it. |
| The user's `PATH` is read from an **interactive** login shell, delimited by sentinels | launchd hands a Finder-launched `.app` `/usr/bin:/bin:/usr/sbin:/sbin`, and the engine and every Session it launches inherit it. The fix reads the user's own shell — but `-lc` was the wrong question: zsh sources `~/.zshrc` only when interactive, and `~/.zshrc` is where `nvm`'s installer and `brew shellenv` actually write. `-i` reaches that page of the ledger; the sentinels are what make an interactive shell's chatter (powerlevel10k's instant prompt) separable from the answer. Same shape VS Code's shell integration uses. `.zprofile` is a steadier home for a `PATH`, but a product that only works for users who already knew that is broken for the majority. |
| `config.example.toml` is shipped, never installed | The configuration is a file the user owns and the engine only reads, and it names adapters by import reference, so it runs with the privileges of whoever wrote it. An installer that authored it would be claiming something that is not the installer's. |

## Regenerating the lock

```bash
scripts/build-app.sh lock
```

It resolves against the **bundled** interpreter's own version and platform,
because a lock resolved by some other Python is a lock for some other set of
wheels. It writes `app_bundle/locks/<triple>.lock`. Read the diff: it is the list
of binaries the next build will sign.

## The v0 acceptance

> **This procedure predates v1.0's scope cut and has not been reshaped yet.**
> Launching and closing Sessions are parked
> ([#72](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/72));
> `bridgectl launch` and `bridgectl close` no longer exist, so every step below
> that runs one is unperformable as written. Read them as the record of the v0
> run they are. The reshaped procedure — the harness starting Sessions through
> the ordinary installed `claude` / `codex` path, the way a user does — is the
> exit condition of
> [#67](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/67) and
> is written there, not here. The findings in this section, including the
> limitations below, are kept as measured.

Everything above is machine-checked on every PR. What follows is not, and cannot
be: it needs a microphone, a person to click a TCC prompt, a real Telegram bot
and two real coding agents. Run it **once, in order, from a bundled build**, on a
machine that is not the one that built it if you can — moving the `.app` is half
of what is being tested.

Before step 0, do the two cutover checks below: retire any first-generation
codex skill, and make sure nothing else is polling this engine's bot. Both are
preconditions rather than steps — get either wrong and the run produces results
that cannot be attributed to this engine at all.

**And before attributing any voice failure to this engine, re-verify the realtime
contract with an engine-free probe:** a bare `codex app-server` client that sends
the v3 realtime start and nothing else. The realtime methods are an alpha backend
surface, absent from the official app-server docs and gated server-side, so the
contract can move without anything here changing — the research that approved
this route said to re-run the probe on every codex bump, and it was right. A
probe that fails identically outside the engine has told you, in seconds, that
the engine is not the subject. On the maintainer's machine this is
`scripts/rt_prototype.py --silent` in the legacy checkout — thirty seconds, no
microphone, no bundle, its own app-server child. The probe is not shipped here;
what is portable is the instruction to run one, and the research resolution it
came from defines its shape.

**0. Configure it, and read *both* failures first.** Copy
`Contents/Resources/config.example.toml` into
`~/Library/Application Support/GPT-VoiceCoding/engine/config.toml`, and before
filling it in properly, break it twice on purpose. The two breakages behave
differently, and knowing which is which is the whole point of the step.

*First, a configuration mistake* — comment out `[delegate] model`. The engine
refuses **before it adopts its log**, so the sentence goes to stderr, the shell's
Retry panel **shows it**, and no `engine.log` is created at all. This is the
pleasant case.

*Then, a missing credential* — put the model back, and start it with the variable
named by `token_env` unset. The engine refuses **after adoption**, so its stderr
*is* the log (ADR 0004): the terminal says nothing, the Retry panel is **empty**,
and the reason — with a full traceback above it — is in `engine.log` beside the
configuration.

That empty panel is a known v0 rough edge. This step exists so you meet it once
on purpose, and it uses the missing-credential case deliberately: it is the most
likely thing to be wrong on anybody's real first run, so you are rehearsing the
failure you would actually have met.

**1. The microphone.** `python3 scripts/microphone_grant_proof.py --reset`, and
follow it. The prompt must name the app.

**2. One bot, one engine.** Check that nothing else is consuming `getUpdates` for
the bot this engine is configured for; if something is, stop it or use a
different bot — see the cutover note below. The `send` half of step 8 will pass
either way; only inbound goes quiet, so this leg proves less than it appears to
if you skip this. On the reference machine there is no contender: the
first-generation bridge's `companionChannel` is empty and it never spoke to
Telegram at all. The constraint is Telegram's and still real; that one bridge is
simply not what would violate it.

**3. Launch a Session.** Twice, by both routes that exist: headless with
`bridgectl launch`, and by voice inside a Live Call started from the menu bar —
the shell deliberately has no launch control of its own, because launching
belongs to the control plane and the call, not to the lifecycle owner. The voice
leg may be performed when the first Live Call comes up and recorded against this
step. Both must reach a real agent in a real workspace, and the headless leg is
also what proves the `PATH` the shell hands the engine is your own and not
launchd's.

A cold launch can take the better part of a minute, and the client now waits up
to 150 s for `launch` alone — a ceiling derived from the launch itself rather
than the ordinary request deadline (#28). So a reported timeout no longer means
"cold launch, be patient": it means the engine genuinely hung. Read the
limitation below before you retry, because retrying the wrong way starts a
second agent.

**4. Let it stop.** Wait for the Session to finish a turn.

**5. The Stop Notice.** With a Live Call up, it must be spoken into the call.

**6. Answer Relay.** Speak an instruction; it must arrive in the Session as your
own words.

**7. Approval Relay.** Get the Session to ask for a permission it needs, and
answer it by voice. One verdict, one request.

A live Session is reachable *only* through the Answer Relay: `launch --task`
carries a label, not an instruction, and a launched Session has no terminal to
type into. Steps 6 and 7 therefore share the fate of the Session Channel — if
the carrier is not there, neither step is performable, and that is a blocked
step rather than a failed one.

**8. The Companion Channel.** A notice must reach Telegram, and a reply typed
there must come back as inbound text. **Run this with Voice off**, together with
step 9's text-only leg — with Voice on it cannot pass, and that is correct
behaviour rather than a fault. The escalation matrix sends a notice to the
Companion Channel only when the call route is shut: with Voice on and no call
up, escalation *opens a call and speaks* instead of pushing. Ending a call with
Voice on therefore produces no push, and must not be recorded as a failure of
this step.

**9. The switches.** Duty off: nothing is spoken and nothing is pushed, but
events are still recorded and the control plane still answers. Voice off,
Message on: text-only operation — this is the leg step 8's Telegram clause is
exercised under. Then back.

**10. Headless.** Every one of the above must be reachable through `bridgectl`
from a terminal, against the same running engine.

## Before a release: the microphone

The one part a machine cannot finish. macOS shows the TCC prompt to a person.

```bash
python3 scripts/microphone_grant_proof.py --reset
```

It prints the checklist and what each step must show. The probe that gated
ADR 0005 already established the negative control — an interpreter outside any
`.app` collapses to the bare binary path — so that half is deliberately not
re-run: it is a property of macOS, not of this bundle.

## Known v0 limitations

Deliberate, and written down so they are not rediscovered as bugs.

**A refusal after log adoption is invisible in the shell.** The engine exits 2
and says why, but by then its stderr *is* its log (ADR 0004), so the menu-bar
shell's Retry panel — which shows only the engine's *pre-adoption* words — is
empty. The answer is `engine.log`, beside the configuration. Step 0 of the
acceptance exists so this is met once on purpose.

**Nothing ends an engine it did not start.** Kill the shell abnormally and the
engine is orphaned, and there is no supported way to stop it but `kill` on the
process. Note this is about the *engine*, not the Sessions it launched: under the
`direct_child` launcher a Session is a direct child of the engine and goes with
its process group, so quitting normally takes the agents with it rather than
leaving them behind. Two reasons, both load-bearing rather than incidental:
`bridgectl` is a *control-plane surface* — status and switches — and giving it a stop verb would
make the control plane a lifecycle owner, which is not what it is; and a
relaunched shell holds no handle on a process it did not spawn, so its Quit
stops its own child and it has none, having refused to start one against the
live socket. `SIGTERM` is what the engine's own signal handling is for: loops
cancelled, socket removed, no debris. If field evidence ever shows the orphan
case is common enough to hurt, that is a reopening with evidence, not a
convenience feature.

**An update may re-prompt for the microphone.** Ad-hoc signatures change per
build. Charter decision 9, accepted; it waits for notarization.

**`bridgectl verify` proves wiring, not that a call can be placed.** Each seam's
verify reports which implementation is loaded and whether that seam's far side
answers — for the Call seam, whether the `codex app-server` responds. It does
**not** establish a realtime session, because a health check that did would open
the microphone, spend a realtime session and need a teardown path. So
`call: pass — the call is down` means "the wiring is sound and no call is
currently up", not "a call can be placed": a refusal that lives further out, at
the realtime backend, is invisible to it. **The first real call attempt is that
proof**, and when it fails the reason is reported verbatim — which is the sentence
to read, and to quote in a bug report.

**Retry with the *same* `--request-id`, never a fresh one.** A launch is held as
a transaction keyed by its request id, so re-issuing the identical command joins
the launch already in flight and returns the Session it produced. A fresh id
describes a *different* launch, and the engine will honour it — starting a
second agent in the same workspace. The identity is the safety mechanism, which
is why the flag is required rather than generated for you.

**A first-generation codex skill silently hijacks the voice thread.** Anyone
upgrading from the first generation has one, and this engine has no way to
refuse it — see the cutover note. Left in place it does not break anything
visibly; it just makes the Live Call drive a control plane that is not here, and
explain itself perfectly while doing so. Retiring the skill is a step in the
cutover rather than a fix in the code, because the file belongs to the user and
so does the decision to keep it.

**Only configured projects can be launched by voice.** #25 added the project
catalogue this entry once deferred: `[[launch.projects]]` in `config.toml` maps
a canonical name and explicit spoken aliases to an absolute workspace, so a cold
workspace *is* now launchable by name — the launch verb takes a project
reference and a task, resolves them through the catalogue, and applies the
configured default agent unless one is named. What remains a limitation is the
catalogue's edge: a workspace with no `[[launch.projects]]` entry cannot be
launched at all — the control-plane launch action refuses raw `workspace` and
`label` fields, and a spoken path resolves to nothing. Adding the entry to
`config.toml` is the supported route, not speaking the path.

## Cutover: retire the first generation's codex skill

**Before the acceptance, and before any real use, check
`~/.codex/skills/` for a first-generation skill and move it out.** On the
reference machine it was `~/.codex/skills/gpt-voicecoding/`, six files, and it
took three launches to notice.

**Check for the first generation's whole runtime too, not only its skill, and
identify it by what it is rather than by where it was last seen.** It has been
found installed at `~/Library/Application Support/GPT-VoiceCoding/runtime/` —
inside *this* product's own directory, which is exactly where an operator will
not think to look, and where a check written against some other address returns
a confident CLEAN. Two tests settle it wherever it turns up: a `bridgectl` whose
verbs are the first generation's (`serve`, `duty-toggle`, `session-label`,
`stop`, `stops`, `install-hooks`) rather than this engine's (`status`,
`switch`, `sessions`, `launch`, `verify`); and a `.source-revision` that
`git cat-file -t` cannot resolve in this repository, which means it was built
from another codebase. An installed runtime whose daemon is not running is
still worth knowing about before you attribute anything.

The Live Call's voice thread runs on a codex app-server, and codex loads skills
from the user's own directory. A skill written for the first-generation bridge
describes a *different* control plane: another binary
(`…/GPT-VoiceCoding/runtime/bridgectl`), verbs this engine does not have
(`launch --list`, `launch --destinations`), and a concept it has no equivalent
for — a catalogue of "shortcuts" mapping a spoken project name to a directory.
The engine cannot prevent this: the skill is the user's file and codex loads it
before any of this engine's own instructions are in play.

**The hijack is silent, which is what makes it expensive.** The model does not
malfunction — it follows the wrong procedure correctly, and its explanations
sound reasonable, because they *are* reasonable under the rules it was given. On
the reference machine it read a retired skill's step 1, called `launch --list`,
got an argparse usage error, and then declined to improvise because that skill's
own rule says a failed launch must not be retried. Every sentence it said was
true of the system it thought it was driving.

The test is the rollout: find the call's rollout under `~/.codex/sessions/`
(its filename carries the Live Call id `bridgectl status` prints) and look for
the first generation's `bridgectl` path or `launch --list`. Either one means the
voice thread is not being driven by this engine's instructions, and nothing it
does can be attributed to this engine until the skill is gone.

## Cutover: one bot, one engine

Telegram permits exactly **one `getUpdates` consumer per bot**. Do not point the
new engine's Companion Channel at a bot something else is already polling: the
two steal each other's inbound messages, `verify` still passes because `getMe`
and `getChat` are unaffected, and only inbound goes quiet. Stop the other
consumer first, or use a distinct bot.

The first-generation bridge was named here as the likely contender, and on this
machine it is not one — its `companionChannel` is configured with an empty
`module` and no credentials, so it has no Telegram channel to hold a bot with.
Anything else polling the same bot — a second engine, a script, another machine —
still would.
