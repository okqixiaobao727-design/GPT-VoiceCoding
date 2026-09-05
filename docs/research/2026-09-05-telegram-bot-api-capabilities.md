# Telegram Bot API capabilities the Telegram map rests on

Research for issue #247 (wayfinder ticket under map #246). Date: 2026-09-05.

Sources, and nothing weaker:

- Reference: https://core.telegram.org/bots/api — fetched 2026-09-05; the page header reads
  "Bot API 10.3" (released August 24, 2026).
- Changelog: https://core.telegram.org/bots/api-changelog — same fetch, used only for the
  version in which a field or method first appeared.

Quotations below are verbatim from those two pages. Where the reference says nothing, the
verdict is SILENT and says so. Two facts are marked *measured*; they come from a live test on
2026-09-05, not from the reference, and are labelled as such.

Verdicts: CONFIRMED (the reference says it), REFUTED (the reference says otherwise), SILENT (the
reference does not address it). Several items carry a mixed verdict per sub-question.

---

## 1. `expandable_blockquote`

**Verdict: CONFIRMED** (syntax, entity type, nesting rule). **SILENT** on a length limit.

Entity type — `MessageEntity.type`:

> “blockquote” (block quotation), “expandable_blockquote” (collapsed-by-default block quotation)

MarkdownV2 syntax — from the "MarkdownV2 style" example block (the `**>` opener is an empty bold
entity followed by `>`; the closing `||` is the "expandability mark"):

```
>Block quotation started
>The last line of the block quotation
**>The expandable block quotation started right after the previous block quotation
>It is separated from the previous block quotation by an empty bold entity
>Expandable block quotation continued
>Hidden by default part of the expandable block quotation started
>Expandable block quotation continued
>The last line of the expandable block quotation with the expandability mark||
```

HTML equivalent: `<blockquote expandable>…</blockquote>`.

Nested formatting inside a quote — "Formatting options":

> Message entities can be nested, providing following restrictions are met:
> - If two entities have common characters, then one of them is fully contained inside another.
> - bold, italic, underline, strikethrough, and spoiler entities can contain and can be part of any other entities, except pre and code.
> - blockquote and expandable_blockquote entities can't be nested.
> - All other entities can't contain each other.

Reading: bold/italic/underline/strikethrough/spoiler may sit inside a blockquote ("can be part
of any other entities"); a blockquote may not contain another blockquote. The reference does
not state separately whether `code`/`pre` may sit inside a blockquote; the rule "All other
entities can't contain each other" applies to the pair (blockquote, pre) since neither is in
the bold/italic/... list, so by that rule they cannot nest. That is an inference from the rule
text, not a sentence of its own.

Length limit on a quote: **SILENT**. No per-quote character limit appears anywhere on the
page; only the message-level limit (item 8) applies.

Legacy `Markdown` parse mode cannot produce it:

> There is no way to specify “underline”, “strikethrough”, “spoiler”, “blockquote”, “expandable_blockquote”, “custom_emoji”, and “date_time” entities, use parse mode MarkdownV2 instead.

Version — changelog, Bot API 7.4 (May 28, 2024):

> Added support for “expandable_blockquote” entities in received messages.
> Added support for “expandable_blockquote” entity parsing in “MarkdownV2” and “HTML” parse modes.
> Allowed to explicitly specify “expandable_blockquote” entities in formatted texts.

Plain `blockquote` arrived earlier, Bot API 7.0 (December 29, 2023): "Added support for
“blockquote” entities in received messages."

Bot API 10.3 also added `RichBlockExpandableBlockQuotation` for rich messages — a separate
mechanism, not needed for `sendMessage` text.

*Measured 2026-09-05 (not reference):* sending MarkdownV2 `**>…||` produced an
`expandable_blockquote` entity on the delivered message.

---

## 2. `reply_to_message` on inbound `Message`; `reply_parameters` / `quote` outbound

**Verdict: CONFIRMED** that the field exists and carries the original `Message`; **SILENT** on
whether the embedded `text` is ever truncated; **CONFIRMED** for outbound `reply_parameters`
and `quote`.

`Message.reply_to_message`:

> reply_to_message | Message | Optional. For replies in the same chat and message thread, the original message. Note that the Message object in this field will not contain further reply_to_message fields even if it itself is a reply. If the message is a reply to an ephemeral message, then this field may be omitted.

"Always present when the user replies?" — the field is *Optional* and scoped to "replies in the
same chat and message thread". Replies across chats or forum topics go to a different field:

> external_reply | ExternalReplyInfo | Optional. Information about the message that is being replied to, which may come from another chat or forum topic

For a private chat with the bot (one chat, one thread) the reference gives no case in which a
reply omits `reply_to_message` other than the ephemeral one quoted above.

Whole or truncated `text`: the embedded object is documented simply as `Message`, whose `text`
is "For text messages, the actual UTF-8 text of the message". The reference says nothing about
truncating the nested message. **SILENT.**

*Measured 2026-09-05 (not reference):* a user reply carried `reply_to_message.message_id`
equal to the bot's sent `message_id`, with the quoted `text` complete for a ~370-character
message.

Inbound partial quote — `Message.quote`:

> quote | TextQuote | Optional. For replies that quote part of the original message, the quoted part of the message

`TextQuote`: `text` ("Text of the quoted part of a message that is replied to by the given
message"), `entities` ("Currently, only bold, italic, underline, strikethrough, spoiler,
custom_emoji, and date_time entities are kept in quotes"), `position` ("Approximate quote
position in the original message in UTF-16 code units"), `is_manual` ("True, if the quote was
chosen manually by the message sender. Otherwise, the quote was added automatically by the
server.").

Outbound — `sendMessage.reply_parameters` of type `ReplyParameters`:

> message_id | Integer | Optional. Identifier of the message that will be replied to in the current chat, or in the chat chat_id if it is specified. Required if ephemeral_message_id isn't specified.
> allow_sending_without_reply | Boolean | Optional. Pass True if the message should be sent even if the specified message to be replied to is not found. …
> quote | String | Optional. Quoted part of the message to be replied to; 0-1024 characters after entities parsing. The quote must be an exact substring of the message to be replied to, including bold, italic, underline, strikethrough, spoiler, custom_emoji, and date_time entities. The message will fail to send if the quote isn't found in the original message. Ignored for ephemeral messages.
> quote_position | Integer | Optional. Position of the quote in the original message in UTF-16 code units

Version — changelog, Bot API 7.0 (December 29, 2023), "Replies 2.0":

> Added the class ReplyParameters and replaced parameters reply_to_message_id and allow_sending_without_reply in the methods copyMessage, sendMessage, … with the field reply_parameters of type ReplyParameters.
> Added the class TextQuote and the field quote of type TextQuote to the class Message …

`reply_to_message` itself: the changelog has no entry adding it — it is not listed as an
addition in any dated entry back to "June 24, 2015 The bot platform is officially launched."

---

## 3. `ForceReply` as `reply_markup`

**Verdict: CONFIRMED** (reply mode to that message; `input_field_placeholder`; `selective`).
**SILENT** on the exact update shape of the next inbound message beyond "replies".

`ForceReply`:

> Upon receiving a message with this object, Telegram clients will display a reply interface to the user (act as if the user has selected the bot's message and tapped 'Reply'). This can be extremely useful if you want to create user-friendly step-by-step interfaces without having to sacrifice privacy mode. Not supported in channels and for messages sent on behalf of a user account.

Fields:

> force_reply | True | Shows reply interface to the user, as if they had manually selected the bot's message and tapped 'Reply'
> input_field_placeholder | String | Optional. The placeholder to be shown in the input field when the reply is active; 1-64 characters
> selective | Boolean | Optional. Use this parameter if you want to force reply from specific users only. Targets: 1) users that are @mentioned in the text of the Message object; 2) if the bot's message is a reply to a message in the same chat and forum topic, sender of the original message.

That the next inbound message arrives as a reply (and so carries `reply_to_message`, item 2)
is stated via the privacy-mode example rather than as a field-level guarantee:

> And if you use ForceReply in your bot's questions, it will receive the user's answers even if it only receives replies, commands and mentions - without any extra work for the user.

The reference does not say the client *prevents* the user from dismissing the reply interface
and sending a non-reply; it describes what the client displays. Treat "next inbound always
carries reply_to_message" as client behaviour, not an API guarantee.

`sendMessage.reply_markup` accepts it:

> reply_markup | InlineKeyboardMarkup or ReplyKeyboardMarkup or ReplyKeyboardRemove or ForceReply | Optional. Additional interface options. A JSON-serialized object for an inline keyboard, custom reply keyboard, instructions to remove a reply keyboard or to force a reply from the user.

New in Bot API 10.3 (August 24, 2026) and relevant to the map: `InlineKeyboardMarkup` and
`ReplyKeyboardMarkup` gained a `force_reply` field, so an inline keyboard and forced reply can
now be combined on one message:

> force_reply | Boolean | Optional. Pass True if the reply interface must be shown to the user, as if they had manually selected the bot's message and tapped 'Reply'. The value of the field can't be changed when the inline keyboard is edited.

Changelog 10.3: "Added the field force_reply to the classes InlineKeyboardMarkup and
ReplyKeyboardMarkup."

Version of `ForceReply` itself: the changelog has no entry adding it; its earliest changelog
mention is Bot API 5.3 (June 25, 2021): "Added the ability to specify a custom input field
placeholder in the classes ReplyKeyboardMarkup and ForceReply."

---

## 4. Inline keyboards, `callback_data`, `callback_query`, `answerCallbackQuery`, `allowed_updates`

**Verdict: CONFIRMED** for markup, 64-byte limit, update field and the answer obligation.
**SILENT** on an answer timeout. **REFUTED** as stated for `allowed_updates` — naming
`callback_query` is required only when a restricting list is passed; by default it is delivered.

`InlineKeyboardMarkup`:

> This object represents an inline keyboard that appears right next to the message it belongs to.
> inline_keyboard | Array of Array of InlineKeyboardButton | Array of button rows, each represented by an Array of InlineKeyboardButton objects

`InlineKeyboardButton.callback_data`:

> callback_data | String | Optional. Data to be sent in a callback query to the bot when the button is pressed, 1-64 bytes

(Bytes, not characters — the reference's own unit.) Bot API 10.3 also added
`InlineKeyboardButton.disabled` (class `DisabledButton`).

`Update.callback_query`:

> callback_query | CallbackQuery | Optional. New incoming callback query

`CallbackQuery`:

> This object represents an incoming callback query from a callback button in an inline keyboard. If the button that originated the query was attached to a message sent by the bot, the field message will be present. … Exactly one of the fields data or game_short_name will be present.
> id | String | Unique identifier for this query
> message | MaybeInaccessibleMessage | Optional. Message sent by the bot with the callback button that originated the query
> data | String | Optional. Data associated with the callback button. Be aware that the message originated the query can contain no callback buttons with this data.

The obligation:

> NOTE: After the user presses a callback button, Telegram clients will display a progress bar until you call answerCallbackQuery. It is, therefore, necessary to react by calling answerCallbackQuery even if no notification to the user is needed (e.g., without specifying any of the optional parameters).

Timeout: **SILENT.** `answerCallbackQuery` documents `callback_query_id`, `text` ("0-200
characters"), `show_alert`, `url`, `cache_time` ("The maximum amount of time in seconds that
the result of the callback query may be cached client-side. Defaults to 0."). No deadline for
answering is stated. (For contrast, the reference does state 10-second deadlines for
`answerPreCheckoutQuery` and join-request queries — so its silence here is deliberate, not a
page-wide omission.)

`getUpdates.allowed_updates`:

> A JSON-serialized list of the update types you want your bot to receive. For example, specify ["message", "edited_channel_post", "callback_query"] to only receive updates of these types. See Update for a complete list of available update types. Specify an empty list to receive all update types except chat_member, message_reaction, and message_reaction_count (default). If not specified, the previous setting will be used.
> Please note that this parameter doesn't affect updates created before the call to getUpdates, so unwanted updates may be received for a short period of time.

So: with no list ever set, or an empty list, `callback_query` is delivered. It must be named
only if the bot passes a non-empty list — and note "If not specified, the previous setting will
be used", i.e. a list set once (by this or any earlier consumer, including via `setWebhook`)
persists.

Versions — changelog:

- Bot API 2.0 (April 9, 2016): "New inline keyboards with callback and URL buttons. Added new objects InlineKeyboardMarkup, InlineKeyboardButton and CallbackQuery, added reply_markup fields to all InlineQueryResult objects. Added field callback_query to the Update object, new method answerCallbackQuery."
- Bot API 2.3.1 (December 4, 2016): "Use allowed_updates in setWebhook and getUpdates to selectively subscribe to updates of a certain type."
- `cache_time` on `answerCallbackQuery`: changelog entry "Added the new parameter cache_time to answerCallbackQuery" (a 2016 entry between the 2.3.1 and 2.1 headings).

---

## 5. `setMyCommands` and the menu button

**Verdict: CONFIRMED** for name rules, scopes and the menu button. **SILENT** on the exact
statement that a menu-chosen command arrives as an ordinary `message` with a `bot_command`
entity — the reference defines the entity and the menu, but never writes that sentence.

`setMyCommands`:

> Use this method to change the list of the bot's commands. See this manual for more details about bot commands. Returns True on success.
> commands | Array of BotCommand | Yes | A JSON-serialized list of bot commands to be set as the list of the bot's commands. At most 100 commands can be specified.
> scope | BotCommandScope | Optional | A JSON-serialized object, describing scope of users for which the commands are relevant. Defaults to BotCommandScopeDefault.
> language_code | String | Optional | A two-letter ISO 639-1 language code. If empty, commands will be applied to all users from the given scope, for whose language there are no dedicated commands.

`BotCommand`:

> command | String | Text of the command; 1-32 characters. Can contain only lowercase English letters, digits and underscores.
> description | String | Description of the command; 1-256 characters
> is_ephemeral | Boolean | Optional. True, if the command sends an ephemeral message, which can be seen only by the sender of the message and the bot

`BotCommandScope`: "Currently, the following 7 scopes are supported": `BotCommandScopeDefault`,
`BotCommandScopeAllPrivateChats`, `BotCommandScopeAllGroupChats`,
`BotCommandScopeAllChatAdministrators`, `BotCommandScopeChat`,
`BotCommandScopeChatAdministrators`, `BotCommandScopeChatMember`. For a private chat the
resolution order under "Determining list of commands" is: `botCommandScopeChat + language_code`,
`botCommandScopeChat`, `botCommandScopeAllPrivateChats + language_code`,
`botCommandScopeAllPrivateChats`, `botCommandScopeDefault + language_code`,
`botCommandScopeDefault` — "The first list of commands which is set is returned."

Menu button — `MenuButton`:

> This object describes the bot's menu button in a private chat. It should be one of MenuButtonCommands, MenuButtonWebApp, MenuButtonDefault.
> If a menu button other than MenuButtonDefault is set for a private chat, then it is applied in the chat. Otherwise the default menu button is applied. By default, the menu button opens the list of bot commands.

`setChatMenuButton`: "Use this method to change the bot's menu button in a private chat, or the
default menu button."

How a command arrives: the reference defines the entity —

> “bot_command” (/start@jobs_bot)

— on `MessageEntity.type`, and `Message.entities` as "For text messages, special entities like
usernames, URLs, bot commands, etc. that appear in the text". `Update.message` is "New incoming
message of any kind - text, photo, sticker, etc." There is no separate update type for commands
in the `Update` field list, and nothing distinguishes a menu-chosen command from a typed one.
The positive sentence the issue asks for does not exist on the page: **SILENT** on that exact
wording, but no contrary mechanism is documented either.

Versions — changelog:

- Bot API 4.7 (March 30, 2020): "Added the method setMyCommands for changing the list of the bot's commands through the Bot API instead of @BotFather." (also `getMyCommands`)
- Bot API 5.3 (June 25, 2021): "Added the class BotCommandScope … Added the parameters scope and language_code to the method setMyCommands … Added the method deleteMyCommands … Improved visibility of bot commands in Telegram apps with the new 'Menu' button in chats with bots".
- Bot API 6.0 (April 16, 2022): "Added the class MenuButton and the methods setChatMenuButton and getChatMenuButton for managing the behavior of the bot's menu button in private chats."

---

## 6. `editMessageText`

**Verdict: CONFIRMED** that it can set an expandable blockquote (any `parse_mode`/`entities`
accepted by `sendMessage`). **SILENT** on an age limit for the bot's own messages and on
whether omitted `entities`/`reply_markup` are preserved.

> Use this method to edit text, rich and game messages. On success, if the edited message is not an inline message, the edited Message is returned, otherwise True is returned. Note that business messages that were not sent by the bot and do not contain an inline keyboard can only be edited within 48 hours from the time they were sent.

Parameters: `business_connection_id`, `chat_id`, `message_id`, `inline_message_id`,

> text | String | Optional | New text of the message, 1-4096 characters after entity parsing; required if rich_message isn't specified
> parse_mode | String | Optional | Mode for parsing entities in the message text. See formatting options for more details.
> entities | Array of MessageEntity | Optional | A JSON-serialized list of special entities that appear in message text, which can be specified instead of parse_mode
> link_preview_options | LinkPreviewOptions | Optional | Link preview generation options for the message
> rich_message | InputRichMessage | Optional | New rich content of the message; required if text isn't specified. …
> reply_markup | InlineKeyboardMarkup | Optional | A JSON-serialized object for an inline keyboard

Age limit: the only 48-hour clause is for *business messages not sent by the bot*. For the
bot's own messages the reference states no edit age limit. (The 48-hour limit that does apply
to the bot's own messages is on `deleteMessage`: "A message can only be deleted if it was sent
less than 48 hours ago.") Two other non-editable cases: paid posts ("can't be edited") and
approved/declined suggested posts ("then it can't be edited").

Entities/markup preserved when omitted: **SILENT.** `parse_mode` and `entities` are documented
identically to `sendMessage`; `reply_markup` accepts only `InlineKeyboardMarkup` (so
`ForceReply` cannot be attached by edit). Nothing says what happens to an existing keyboard or
existing entities when the parameter is left out.

Adding or changing an expandable blockquote: since `parse_mode` refers to the same "formatting
options" as `sendMessage`, which lists the `**>…||` syntax (item 1), an edit may carry one.
The reference does not single this out; it follows from the shared parameter definition.

Version — Bot API 2.0 (April 9, 2016): "Bots can now edit their messages. Added methods
editMessageText, editMessageCaption, editMessageReplyMarkup."

---

## 7. MarkdownV2 escaping

**Verdict: CONFIRMED.** The complete rule set, verbatim from "MarkdownV2 style — Please note":

> Any character with code between 1 and 126 inclusively can be escaped anywhere with a preceding '\' character, in which case it is treated as an ordinary character and not a part of the markup. This implies that '\' character usually must be escaped with a preceding '\' character.
> Inside pre and code entities, all '`' and '\' characters must be escaped with a preceding '\' character.
> Inside the (...) part of the inline link and custom emoji definition, all ')' and '\' must be escaped with a preceding '\' character.
> In all other places characters '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!' must be escaped with the preceding character '\'.
> In case of ambiguity between italic and underline entities __ is always greedily treated from left to right as beginning or end of an underline entity, so instead of ___italic underline___ use ___italic underline_**__, adding an empty bold entity as a separator.

So the "outside entities" list is the 18 characters `_ * [ ] ( ) ~ ` > # + - = | { } . !`.
Inside a `code` span or `pre` block only two characters need escaping: a literal backtick is
written `` \` `` and a literal backslash `\\`. Inside a link/emoji `(...)` part: `\)` and `\\`.

Note the phrase "in all other places": bold, italic, underline, strikethrough, spoiler and
blockquote bodies are *not* exempt — the 18-character list applies inside them as well; only
`pre`/`code` and the link `(...)` part have their own shorter lists.

Version — Bot API 4.5 (December 31, 2019): "Added a new parse mode, MarkdownV2, which supports
nested entities and two new entities __ (for underlined text) and ~ (for strikethrough text).
Parse mode Markdown remains unchanged for backward compatibility."

---

## 8. The 4096-character cap

**Verdict: CONFIRMED** for the number and that markup is excluded. **SILENT** on the counting
unit for the cap and on refuse-vs-truncate.

`sendMessage.text`:

> text | String | Yes | Text of the message to be sent, 1-4096 characters after entities parsing

`editMessageText.text`: "New text of the message, 1-4096 characters after entity parsing".

"After entities parsing" means the markup characters (`*`, `\`, `**>`, `||`, HTML tags) are
not counted — the limit applies to the resulting plain text. Entity *metadata* does not count;
the text *inside* an entity is still text and does count.

Unit: the reference says "characters" for the cap and does not define the term there. The only
place it names a unit is `MessageEntity`: "offset | Integer | Offset in UTF-16 code units to
the start of the entity" and "length | Integer | Length of the entity in UTF-16 code units";
`ReplyParameters.quote_position` and `TextQuote.position` are also "in UTF-16 code units".
Whether the 4096 cap uses the same unit is **not stated**. Do not rely on either reading for
text near the limit without measuring.

Refuse or truncate: **SILENT.** The generic error contract is the only statement: "In case of
an unsuccessful request, 'ok' equals False and the error is explained in the 'description'. An
Integer 'error_code' field is also returned, but its contents are subject to change in the
future." No text on the page says an over-long `text` is truncated. The strings "too long" and
"truncat" do not occur on the page.

Version: the changelog has no entry introducing or changing the 4096 figure.

---

## 9. `getUpdates` vs webhook; the Conflict error

**Verdict: CONFIRMED** mutually exclusive. **SILENT** on a `Conflict` error and on what
happens when two consumers poll one bot.

"Getting updates":

> There are two mutually exclusive ways of receiving updates for your bot - the getUpdates method on one hand and webhooks on the other. Incoming updates are stored on the server until the bot receives them either way, but they will not be kept longer than 24 hours.

`getUpdates` notes:

> 1. This method will not work if an outgoing webhook is set up.
> 2. In order to avoid getting duplicate updates, recalculate offset after each server response.

`deleteWebhook`: "Use this method to remove webhook integration if you decide to switch back to
getUpdates."

Confirmation semantics that matter for two pollers — `getUpdates.offset`:

> Identifier of the first update to be returned. Must be greater by one than the highest among the identifiers of previously received updates. By default, updates starting with the earliest unconfirmed update are returned. An update is considered confirmed as soon as getUpdates is called with an offset higher than its update_id. …

Two consumers polling one bot: the word "Conflict" does not appear on the reference page (zero
occurrences), nor does any HTTP 409 or "terminated by other getUpdates request" text. The
documented error vocabulary is `ok`, `description`, `error_code` ("contents are subject to
change") and `ResponseParameters` (`migrate_to_chat_id`, `retry_after`). Any specific
`Conflict` wording a wrapper reports is therefore observed server behaviour, not a documented
contract; the map should not depend on its text or code. **SILENT.**

Version — Bot API 2.3.1 (December 4, 2016): "deleteWebhook moved out of setWebhook to get a
whole separate method for itself." The mutual-exclusion rule has no changelog entry of its own.

---

## Summary

| # | Item | Verdict | One-line fact |
|---|------|---------|---------------|
| 1 | `expandable_blockquote` | CONFIRMED (limit: SILENT) | MarkdownV2 `**>…||`, HTML `<blockquote expandable>`, Bot API 7.4; blockquotes cannot nest; no quote length limit stated |
| 2 | `reply_to_message` / `reply_parameters` | CONFIRMED (truncation: SILENT) | Optional field, same chat+thread only, nested `Message` without its own `reply_to_message`; `ReplyParameters.quote` 0-1024 chars, Bot API 7.0 |
| 3 | `ForceReply` | CONFIRMED | Client shows reply UI to that message; `input_field_placeholder` 1-64 chars (5.3); `selective`; 10.3 adds `force_reply` on `InlineKeyboardMarkup` |
| 4 | Inline keyboards / callbacks | CONFIRMED; `allowed_updates` claim REFUTED; timeout SILENT | `callback_data` 1-64 bytes; must call `answerCallbackQuery` (no deadline stated); `callback_query` delivered by default, must be named only in a non-empty `allowed_updates`; Bot API 2.0 / 2.3.1 |
| 5 | `setMyCommands` / menu | CONFIRMED (arrival shape: SILENT) | `command` 1-32 chars lowercase letters, digits, underscores; 100 max; 7 scopes; `bot_command` entity defined; 4.7 / 5.3 / 6.0 |
| 6 | `editMessageText` | CONFIRMED (age, preservation: SILENT) | Same `parse_mode`/`entities` as send, so blockquotes editable; 48 h limit only for business messages not sent by the bot; `reply_markup` inline only; Bot API 2.0 |
| 7 | MarkdownV2 escaping | CONFIRMED | 18 chars outside pre/code; inside pre/code only `` ` `` and `\`; inside link `(...)` only `)` and `\`; Bot API 4.5 |
| 8 | 4096 cap | CONFIRMED (unit, refuse/truncate: SILENT) | "1-4096 characters after entities parsing"; entity offsets are UTF-16 but the cap's unit is not stated; no truncation documented |
| 9 | `getUpdates` vs webhook | CONFIRMED (Conflict: SILENT) | "two mutually exclusive ways"; `getUpdates` "will not work if an outgoing webhook is set up"; the word Conflict does not appear |

## Contradictions with the map's assumptions

1. `allowed_updates` need not name `callback_query`; it is delivered by default. It *must* be
   named only if the bot passes a restricting list — and a previously set list persists
   ("If not specified, the previous setting will be used").
2. No `answerCallbackQuery` deadline exists in the reference; the client shows a progress bar
   until answered, that is all.
3. No `Conflict` error is documented; do not match on its text or code.
4. The 4096 cap's unit and the refuse-vs-truncate behaviour are not documented; only entity
   offsets are specified in UTF-16.
5. `editMessageText.reply_markup` accepts `InlineKeyboardMarkup` only — a `ForceReply` cannot be
   added by editing. Since 10.3, `InlineKeyboardMarkup.force_reply` offers the combination on
   send, but "can't be changed when the inline keyboard is edited".
6. No per-quote length limit exists for blockquotes; only the message cap applies.
