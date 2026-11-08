# sazed-agent

The mind of **sazed**, a personal knowledge base you feed and query from a Telegram chat.

It lives on your own computer and keeps a thread open towards
[`sazed-bot`](https://github.com/flongo82/sazed-bot), the always-open door on Cloudflare.
Through it comes every message: the agent reads it all the way through (it transcribes
videos and voice messages, extracts the text of pages and documents, looks inside photos),
understands whether it is something to keep, a question, or both, and answers in the same
chat.

What deserves to be remembered goes into **coppermind**, the catalogue: like the copper minds
of the Terris Keepers in Brandon Sanderson's *Mistborn*, it holds what is entrusted to it so
that it can be found again.

## Status

Being built. Done so far: configuration, the local journal of incoming messages (durable,
idempotent, with states that only move forward and limited retries), the text catalog in
Italian and English, and the connection with `sazed-bot`: every message is written to the
journal before it is acknowledged, duplicates are recognised, and the agent reconnects by
itself after a dropped or hung connection or after the computer wakes up from sleep. The
status command answers at once, even while a long job is running; failed attempts are retried
with growing delays, and a message that keeps failing is set aside with a notice, without
blocking the others. While the agent is connected the status command reaches it, and the
Worker answers only for an agent that is off or silent. A contract test runs the agent against
the real Worker code on this computer, and a probe has shown that the bot downloads through
MTProto files beyond the Bot API's 20 MB while the webhook keeps working.

Every message gets 👍 as soon as it arrives: it has been taken in charge, and nothing else is
said about what you keep. Every link becomes an item of its own, with its platform (YouTube,
Instagram, TikTok…) and its site; short links (share.google, bit.ly…) are followed to their
true address first. Photos, videos, voice messages, audio and documents are kept as references
to their message in the chat. The same link or file sent twice stays one item, and an edited
message updates its items. Searches work: complete lists with exact filters (kind, platform,
site, kind of input, dates) and full-text search that ignores accents and matches word
beginnings.

Claude (Haiku, through the API) reads the words of each message together with the recent
conversation, and tells what they are for: a note to keep, a comment on something you sent
(in the same message, or a link sent just before, or a message you replied to), a question or
a request. The code keeps notes and comments; links and files are kept in any case, before
Claude is asked. Questions get an answer in the chat: Claude looks into coppermind with two
read-only tools (search with exact filters and words, and one item with the text read at its
source). A question about a link waits until that link has been read, for a few minutes at
most. Words spoken in a voice message (or a round video message) count like typed ones: once
transcribed, they go through the same reading, so a question asked aloud gets its answer, and a
voice message that only asks or comments is not kept. A forwarded voice message is content,
not your words.

Links and files are read in a lane of their own, one at a time, so that nothing else waits
for them. Pages give their title, description, structured data (recipes with ingredients and
steps) and main text; Instagram and TikTok give their whole caption, and YouTube its title and
channel, through their public previews, together with the cover image: Claude looks at it,
which is how a reel without a caption still gets a title. YouTube videos also give, through
yt-dlp, their description and their subtitles in the original language (never automatic
translations), or, when there are none, their audio for Whisper. Files sent in the chat (up to the
Bot API's 20 MB) are downloaded into a temporary folder that disappears right after: Claude
looks at photos; voice messages, audio and videos are transcribed on your computer by
whisper.cpp, on the processor and at low priority, and a video also gives four frames for
Claude to look at. From what was read, Claude describes each item with a kind (recipe,
article, video, note…), a title, topics and a short summary, which coppermind indexes; what
was read is data for it, never instructions. A source that cannot be read gets 🤷 on its
message. Documents (PDF, EPUB, DOCX, ODT, plain text) are only glanced at unless you ask for
more: their file data and first two pages give the title, the authors and, for a paper, the
abstract; a scan shows its first page to Claude. When the first pages do not say what a published
work is about, its card is looked up in free catalogues (Open Library, Crossref, Wikipedia), and
Claude uses it only if it speaks of the same work; personal documents are never looked up.
Ask to read a document in full, with it or later (replying to it, or naming it), and the whole
text is read, summarised as a whole and searchable passage by passage; the answer comes once the
reading is done. Ask for a file you kept ("send me the rent contract") and, after the answer, the
bot copies its original message back into the chat, or resends the file if that message is gone.
Next: files beyond 20 MB.
The architecture, the reasons behind it and the protocol between the two parts are described
in the README of `sazed-bot`.

## Getting started

You need `sazed-bot` already set up (its `secrets/` folder holds the agent token and the
Worker's address), Podman, and your bot token.

```bash
scripts/setup.sh          # copies the agent token and the Worker's address from ../sazed-bot,
                          # asks for the bot token, writes config.toml
scripts/setup-mtproto.sh  # asks for the api_id and api_hash of an application created on
                          # my.telegram.org, to download files beyond the Bot API's 20 MB
scripts/setup-claude.sh   # asks for a Claude API key from the Claude Console, checks it with
                          # the API and saves it; the model is chosen in config.toml
scripts/setup-whisper.sh  # downloads the whisper.cpp model into models/ (about 574 MB) and
                          # checks it against its known SHA-256
scripts/build.sh          # builds the sazed-agent image (podman rmi sazed-agent removes it)
scripts/test.sh           # runs the tests in the container, without network access
scripts/contract-test.sh  # the agent against the Worker's code in wrangler dev, with Node and
                          # wrangler from ../sazed-bot (read-only) and no network access
scripts/run.sh            # starts the agent (podman stop sazed-agent stops it)
scripts/mtproto-probe.sh  # experiment, with the agent on: the bot downloads a file you send
                          # beyond 20 MB through MTProto, while the webhook keeps working
```

Through MTProto the agent logs in with the bot token, never with your account: `api_id` and
`api_hash` only identify the application. Its session is the one secret the agent writes, in
`secrets/mtproto/`, the only part of `secrets/` mounted writable.

An API key is not tied to a model: the agent names the model on every request, from the
`[claude]` section of `config.toml` (Claude Haiku by default, the cheapest, which is plenty
for understanding your messages, describing what it reads and answering questions). The key comes from the Claude
Console, which bills separately from a claude.ai subscription; some plans include monthly API
credits for it. Create a key used only by this agent, so that you can revoke it on its own.

`python3 -m sazed_agent check` verifies configuration, secrets, journal and coppermind without
connecting to anything, and says whether Claude and the Whisper model are ready. Without the
model, voice messages and videos are kept and wait to be heard.

Run one agent per bot. If another agent connects to the same Worker, the Worker closes this
one's connection (code 4000) and this one stops with exit code 2 instead of fighting back: two
agents would split your messages between two journals. To move the agent to another
computer, stop it here first.

## How it will work

| Part | How |
|---|---|
| Connection | WebSocket client towards `sazed-bot`: backlog on start, periodic `ping`, `ack` once a message is safely recorded in the local journal |
| Reliability | Every message goes through persistent states (received, read, saved, answered, done, or failed), and every link through its own reading state, with limited retries; one broken item never blocks the others |
| Agent | The code saves, the model interprets: Claude says what your words are for and describes what was read, as structured data that the code checks and writes; to answer questions it has read-only tools on coppermind, and the recent conversation |
| Links | Short links followed to their true address; pages read for title, description, structured data and main text; public previews for Instagram, TikTok and YouTube, cover included; description and subtitles of YouTube videos through yt-dlp; never an address inside your home network |
| Videos and voice messages | ffmpeg for the audio and a few frames, whisper.cpp on the processor for the transcript; yt-dlp for the subtitles and audio of YouTube |
| Pages and documents | Main-text extraction; MarkItDown and Docling for PDFs and documents |
| Photos | Claude looks at them and reads the words in them; place and date of the shot from EXIF, with reverse geocoding on OpenStreetMap |
| Large files | Download through MTProto, up to 2 GB |
| coppermind | A single SQLite file, shared with the journal so that saving an item and moving its message forward are one transaction: exact filters, full-text search (FTS5), and semantic search with embeddings computed on the GPU |

Only links, metadata and extracted text are kept: the sources stay where they are, and your
files stay in the Telegram chat, which coppermind points back to.

## Make it yours

sazed is the name of the software. Your bot's name, its username and the name it introduces
itself with are yours to choose, and none of them will live in this repository: personal
values and secrets stay in files that git ignores.

The agent runs in a rootless Podman container: everything it needs lives in the image (Ubuntu
packages, plus the few that apt lacks, pinned with their hashes in `pip-requirements.txt`), and
your computer only needs Podman (plus NVIDIA's container toolkit once transcription uses the
GPU). Moving the agent to another computer means copying this folder, with its `data/` and
`secrets/`, and starting the container there.

## License

MIT: see [LICENSE](LICENSE).
