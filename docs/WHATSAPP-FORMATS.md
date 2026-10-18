# WhatsApp export formats handled

Stage s1 (`field_report/stages/s1_parse_chat.py`, grammar in `field_report/lib/common.py`) reads the `.txt` file from WhatsApp's "Export chat" and writes `messages.json`. The format is detected from the header lines and recorded as `android` or `ios`.

## Header grammars

| | Android | iOS |
|---|---|---|
| Header | `14/01/2030, 9:02 am - Alex Demo: text` | `[14/01/2030, 09:02:11] Alex Demo: text` |
| Date | day first, `DD/MM/YYYY` | day first, `DD/MM/YYYY` |
| Clock | 12-hour with `am`/`pm` (a narrow no-break space before it is accepted) | 24-hour with seconds |
| Attachment | `IMG-20300114-WA0001.jpg (file attached)` | `<attached: IMG-20300114-WA0001.jpg>` |
| Location pin | `location:` followed by a Google Maps link whose `q` parameter is `LAT,LNG` | same text, prefix matched case-insensitively |
| Media omitted | `<Media omitted>` | `<Media omitted>`, or `<image omitted>`, `<audio omitted>` and similar |
| System lines | no sender, so recognised by structure | carry a sender name, so recognised by wording (encryption notice, group created, added, removed, left, name or icon changed, disappearing messages, security code changed) |

iOS lines for attachments and system notices may start with U+200E or U+200F; these are stripped before matching. U+2068 and U+2069 (wrapping @mentions) are stripped from text.

Month-first dates are not supported. Locales that change the header layout are not handled.

## Message kinds

Each message gets one `kind`: `text`, `media`, `location`, `media_omitted` or `system`.

- A line that does not match a header is a continuation: it is appended to the previous message with a newline.
- Kind is decided from the first physical line only, so a continuation that repeats a file name does not change it.
- A trailing ` <This message was edited>` is removed from the text and sets `edited`.
- `This message was deleted` and `You deleted this message` set `deleted`.
- A `location:` line whose coordinates do not parse is recorded as an `UNPARSABLE_LINE` error and the run continues (status `partial`).
- A lone `.` message is a stop separator for s5 (`.`, `。` and `·` are accepted).
- The sender name is kept as it appears in the export. Gate check G5 looks for named individuals in the deliverables.

## Try it

`python examples/make_sample.py --out examples/sample-ios --format ios --tts none` writes the same synthetic walk in the iOS grammar. The Android version is `examples/sample/export/WhatsApp Chat with Demo Site Walk.txt`. Both exercise each branch above: a `<Media omitted>` line, an edited message, a deleted message and a multi-line message.
