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
| `bridgectl` is a two-line wrapper, not pip's console script | `pip` writes an **absolute** shebang. The interpreter relocates; its scripts do not. The engine's own check on `[delegate] cli` is "is a runnable file", which a dead shebang passes — so the failure would surface as `bad interpreter` inside a generated instruction. |
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

Everything above is machine-checked on every PR. What follows is not, and cannot
be: it needs a microphone, a person to click a TCC prompt, a real Telegram bot
and two real coding agents. Run it **once, in order, from a bundled build**, on a
machine that is not the one that built it if you can — moving the `.app` is half
of what is being tested.

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

**2. One bot, one engine.** If the first-generation bridge still runs here, stop
it or point this engine at a different bot — see the cutover note below. The
`send` half of step 8 will pass either way; only inbound goes quiet, so this leg
proves less than it appears to if you skip this.

**3. Launch a Session.** From the menu bar, and again headless with
`bridgectl launch`. Both must reach a real agent in a real workspace — this is
also the step that proves the `PATH` the shell hands the engine is your own and
not launchd's.

**4. Let it stop.** Wait for the Session to finish a turn.

**5. The Stop Notice.** With a Live Call up, it must be spoken into the call.

**6. Answer Relay.** Speak an instruction; it must arrive in the Session as your
own words.

**7. Approval Relay.** Get the Session to ask for a permission it needs, and
answer it by voice. One verdict, one request.

**8. The Companion Channel.** End the call. A notice must reach Telegram, and a
reply typed there must come back as inbound text.

**9. The switches.** Duty off: nothing is spoken and nothing is pushed, but
events are still recorded and the control plane still answers. Voice off,
Message on: text-only operation. Then back.

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
process. Two reasons, both load-bearing rather than incidental: `bridgectl` is a
*control-plane surface* — status and switches — and giving it a stop verb would
make the control plane a lifecycle owner, which is not what it is; and a
relaunched shell holds no handle on a process it did not spawn, so its Quit
stops its own child and it has none, having refused to start one against the
live socket. `SIGTERM` is what the engine's own signal handling is for: loops
cancelled, socket removed, no debris. If field evidence ever shows the orphan
case is common enough to hurt, that is a reopening with evidence, not a
convenience feature.

**An update may re-prompt for the microphone.** Ad-hoc signatures change per
build. Charter decision 9, accepted; it waits for notarization.

## Cutover: one bot, one engine

Telegram permits exactly **one `getUpdates` consumer per bot**. If the
first-generation bridge is still running on the same machine, do not point the
new engine's Companion Channel at the bot it holds: the two steal each other's
inbound messages, `verify` still passes because `getMe` and `getChat` are
unaffected, and only inbound goes quiet. Stop the old bridge first, or use a
distinct bot until it is retired.
