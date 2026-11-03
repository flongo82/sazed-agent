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

Every message now ends up in coppermind: it gets 👀, is saved, then gets a reply and 👌. A note
is kept as written; every link becomes an item of its own, with its platform (YouTube,
Instagram, TikTok…), its site and the words around it as a note; photos, videos, voice
messages, audio and documents are kept as references to their message in the chat. The same
link or file sent twice stays one item, and an edited message updates its items. The reply
("I saved the link") is written by the code after the commit, never before. Searches already
work: complete lists with exact filters (type, platform, site, kind of input, dates) and
full-text search that ignores accents and matches word beginnings.

Next: Claude tells notes from questions, classifies what is saved and answers from coppermind;
then the agent starts reading pages, videos and files. The architecture, the reasons behind it
and the protocol between the two parts are described in the README of `sazed-bot`.

## Getting started

You need `sazed-bot` already set up (its `secrets/` folder holds the agent token and the
Worker's address), Podman, and your bot token.

```bash
scripts/setup.sh          # copies the agent token and the Worker's address from ../sazed-bot,
                          # asks for the bot token, writes config.toml
scripts/setup-mtproto.sh  # asks for the api_id and api_hash of an application created on
                          # my.telegram.org, to download files beyond the Bot API's 20 MB
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

`python3 -m sazed_agent check` verifies configuration, secrets, journal and coppermind without
connecting to anything.

Run one agent per bot. If another agent connects to the same Worker, the Worker closes this
one's connection (code 4000) and this one stops with exit code 2 instead of fighting back: two
agents would split your messages between two journals. To move the agent to another
computer, stop it here first.

## How it will work

| Part | How |
|---|---|
| Connection | WebSocket client towards `sazed-bot`: backlog on start, periodic `ping`, `ack` once a message is safely recorded in the local journal |
| Reliability | Every message goes through persistent states (received, read, saved, answered, done, or failed), with limited retries; one broken item never blocks the others |
| Agent | The code saves, the model interprets: Claude returns intent and classification as structured data, the code writes and checks; to answer questions Claude has read-only tools to search and send back saved messages |
| Videos and voice messages | yt-dlp for the audio, faster-whisper on the GPU for the transcript |
| Pages and documents | Main-text extraction; MarkItDown and Docling for PDFs and documents |
| Photos | OCR; place and date of the shot from EXIF, with reverse geocoding on OpenStreetMap |
| Large files | Download through MTProto, up to 2 GB |
| coppermind | A single SQLite file, shared with the journal so that saving an item and moving its message forward are one transaction: exact filters, full-text search (FTS5), and semantic search with embeddings computed on the GPU |

Only links, metadata and extracted text are kept: the sources stay where they are, and your
files stay in the Telegram chat, which coppermind points back to.

## Make it yours

sazed is the name of the software. Your bot's name, its username and the name it introduces
itself with are yours to choose, and none of them will live in this repository: personal
values and secrets stay in files that git ignores.

The agent runs in a rootless Podman container: everything it needs lives in the image, and
your computer only needs Podman (plus NVIDIA's container toolkit once transcription uses the
GPU). Moving the agent to another computer means copying this folder, with its `data/` and
`secrets/`, and starting the container there.

## License

MIT: see [LICENSE](LICENSE).
