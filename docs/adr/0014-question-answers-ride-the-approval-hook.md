# 14. A question answer rides the Approval Relay as a typed verdict

Date: 2026-08-28 · Status: Accepted · Source: [#103](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/103)

ADR 0013 closes the Session inbox to answers to a Session's own question: the inbox carries peer words, not the user's authority, and Claude Code refuses to treat them as the user's answer. `AskUserQuestion` raises the same `PermissionRequest` hook as a permission dialog, however, and its payload carries the whole question plus `prompt_id`. On Claude Code 2.1.246, returning a denial whose message is the answer places that message in the Session as the tool result and the Session continues on it. The hook is therefore the route that exists; what was missing was a seam value able to name an answer without pretending it was allow or deny.

Legacy had no approval transport and never answered a question remotely (`legacy@1d32845:bridge/daemon.py:1901-2052`). This is **new behaviour**, not a port.

## Decision

**`ApprovalVerdict` is a typed immutable value with four kinds: `allow`, `deny`, `ask`, and `answer`.** The first three carry no text. `answer` carries non-blank text. This is one closed value on the existing Approval Relay rather than a second question-answer verb or a union of unrelated types, so the Agent seam still has one way to resolve the hook it is holding.

**The Control Plane keeps the existing three bare strings and gives an answer an explicit shape.** `"allow"`, `"deny"`, and `"ask"` remain valid verdict payloads. Free text is accepted only as `{"kind":"answer","text":"..."}`; an arbitrary string is not reinterpreted as an answer. This keeps a misspelt permission verdict from silently becoming words sent into a Session.

**The shared line command spells that tag as `approve <approval-id> answer <words...>`.** Every argument after `answer` is joined at its argument boundary with one space and becomes `text`; there is no additional trimming and no comparison with the offered options. The existing `approve <approval-id> allow|deny|ask` forms stay unchanged, and any other would-be kind is a parse error. `bridgectl`, the Companion Channel, and a Live Call's Delegated Turn all use this one grammar and one generated help string.

**Bridge Core matches the verdict shape to what is waiting.** A permission accepts `allow`, `deny`, or `ask`; a question accepts `answer(text)` or `ask`. Text for a permission and allow/deny for a question are explicit refusals. The pending request therefore carries whether it is a permission or a question across the Agent seam; adapters carry the decision but never invent or validate policy.

**An option selection is carried as its label verbatim, and the Session interprets it.** Bridge Core does not correlate the answer with an offered option and does not reject the user's own words for failing to match one. A surface choosing an option puts that option's label in `answer(text)` unchanged. The Claude adapter renders `answer(text)` as the measured hook decision `deny` with `message` equal to that text byte for byte; it does not prepend an explanation or replace it with the bridge's denial prose.

**Delivery proof does not change.** Hook output shape drift is silent, so the `approval_ack` frame on the bridge's own socket remains the only positive proof that the hook received the verdict. Hook exit is not a receipt. `ask` still prints nothing and hands the question or permission back to the on-screen dialog.

**The Control Panel renders pending questions from the existing Control Plane state.** It does not add a second queue or change the Session roster. For each pending `question`, it shows the carried prompt and option labels. An option button sends that label unchanged as `answer(text)`; a free-text field sends the typed words through the same shape. Permissions remain outside this minimal question-answer panel.

## Consequences

The Approval Pipeline now owns pending questions as well as pending permissions, including their common budget, exactly-once resolution, and closing notice. A question's closing notice describes an answered question rather than an approved or denied permission.

The Agent seam, Control Plane document, hook socket document, and every adapter or fake implementing `approval_relay` carry the same typed verdict. Codex has no question hook route, so it can refuse the answer honestly without creating a second transport.

The Swift shell reads `approval_id`, `kind`, `prompt`, and `options` from `status.pending_approvals`, and posts the same structured verdict as every other Control Plane client. The roster remains view-only and is not involved in answering.

ADR 0013's routing boundary stands: ordinary Answer Relays still use the Session inbox, while an answer to the Session's own pending question uses the `PermissionRequest` hook because it must arrive with the user's authority.
