# sazed-agent

The mind of **sazed**, a personal knowledge base you feed and query from a Telegram chat.

It lives on your own computer and keeps a thread open towards
[`sazed-bot`](https://github.com/flongo82/sazed-bot), the always-open door on Cloudflare.
Through it comes every message: the agent reads it all the way through (it transcribes
videos and voice messages, extracts the text of pages and documents, looks inside photos),
understands whether it is something to keep, a question, or both, and answers in the same
chat.

What deserves to be remembered goes into **coppermind**, the catalogue, so that it can be found
again.

In Brandon Sanderson's *Mistborn*, Sazed is a Terrisman Keeper who stores knowledge in
copperminds and draws it out again whenever someone needs it: hence the names. This repository
is the Keeper's mind; coppermind, what it keeps.

## What it does

**Keeps what you send.** Every message gets 👍 as soon as it arrives, and nothing else is said
about what you keep. Each link becomes an item, with its platform and site; short links
(share.google, bit.ly…) are followed to their true address first. Photos, videos, voice
messages and documents stay in the Telegram chat, and coppermind points back to their message.
The same link or file sent twice stays one item; an edited message updates its items.

**Understands your words in the conversation.** Claude reads each message with the recent
conversation and tells what the words are for: a note, a comment on something you sent (in
the same message, just before, or in a message you replied to), a question or a request. Words
spoken in a voice message count like typed ones, so a question asked aloud gets its answer.

**Reads the sources**, one at a time, in a lane of its own:

- web pages: title, description, structured data (recipes with ingredients and steps) and
  main text;
- Instagram, TikTok and YouTube: their public previews with the cover image, which Claude
  looks at; for YouTube, also the description and the subtitles in the original language
  through yt-dlp, or the audio when there are none; for an Instagram reel whose caption and
  cover say too little, also its video, asked for without an account when Instagram gives it,
  transcribed and read through its frames;
- files sent in the chat: photos are looked at by Claude; voice messages, audio and videos are
  transcribed on your computer by whisper.cpp, and the writing on a video is read on your
  computer too: tesseract reads a frame every second, repeated captions become one, and Claude
  gets that text with a frame for each different caption, to read it cleanly. In a reel without
  sound or caption the writing is often all it says;
- documents (PDF, EPUB, DOCX, ODT, plain text): only their file data and first pages unless
  you ask for more, enough for the title, the authors and a paper's abstract. When a published
  work's first pages do not say what it is about, its card is looked up in free catalogues
  (Open Library, Crossref, Wikipedia); personal documents are never looked up. Ask to read a
  document in full, with it or later, and the whole text is read and summarised.

From what was read, Claude gives each item a kind, a title, its authors, topics and a short
summary, and the words written in its images (a photo, a screenshot, the captions of a video),
which coppermind indexes with the text. A source that cannot be read gets 🤷 on its message. When
sazed cannot tell what something is about, and you wrote nothing with it, it says so and asks you
for a few words: they are kept with it. A message set aside after its attempts, or a source that
could not be read because of a passing problem, comes with a Retry button that starts it over.

**Answers questions** in the chat, looking into coppermind with read-only tools: exact
filters and words, meaning (so "desserts that need no oven" finds a tiramisù, even saved with
other words or in another language), one item with its text, the passages of a long text. Ask for a file you
kept ("send me the rent contract") and, after the answer, the bot copies its original message
back into the chat.

## Status

Working end to end with `sazed-bot`, and it can run as a service of your user. Not there yet:
the place and date of photos (EXIF), and backups of the catalogue.

## Getting started

You need `sazed-bot` already set up (its `secrets/` folder holds the agent token and the
Worker's address), rootless Podman, and your bot token.

```bash
scripts/setup.sh          # copies the agent token and the Worker's address from ../sazed-bot,
                          # asks for the bot token, writes config.toml
scripts/setup-claude.sh   # asks for a Claude API key from the Claude Console, checks it with
                          # the API and saves it; the model is chosen in config.toml
scripts/setup-whisper.sh  # downloads the whisper.cpp model into models/ (about 574 MB) and
                          # checks it against its known SHA-256
scripts/setup-embeddings.sh  # downloads the embedding model into models/ (about 639 MB) and
                          # checks it against its known SHA-256
scripts/setup-instagram.sh <cookies.txt>  # keeps the instagram.com cookies of a secondary
                          # account, exported from a browser, in secrets/
scripts/setup-mtproto.sh  # asks for the api_id and api_hash of an application created on
                          # my.telegram.org, to download files beyond the Bot API's 20 MB
scripts/build.sh          # builds the sazed-agent image (podman rmi sazed-agent removes it)
scripts/test.sh           # runs the tests in the container, without network access
scripts/run.sh check      # verifies configuration and secrets, without connecting to anything
scripts/run.sh            # starts the agent by hand (podman stop sazed-agent stops it)
scripts/install-service.sh  # runs it as a service of your user instead (Podman Quadlet and
                          # systemd): it starts at login, restarts if it fails; --remove removes it
scripts/contract-test.sh  # the agent against the Worker's code in wrangler dev, with Node and
                          # wrangler from ../sazed-bot (read-only) and no network access
```

Each of Claude, Whisper, the embedding model and MTProto is optional: without Claude the
words go with the items or become notes, and nothing is described or answered; without the
Whisper model, voice messages and videos are kept and wait to be heard; without the embedding
model, search goes by words only; without MTProto, files beyond 20 MB get 🤷.

Without an account, Instagram gives a reel's video only sometimes; when it does not, the
caption and cover remain, and sazed may suggest downloading the reel from the app (Share →
Download) and sending it as a video. The cookies of a secondary account are optional: Instagram
may limit or suspend an account it sees used by a program, so never use your own. To use one,
log in with it in a browser, export the instagram.com cookies in Netscape format (an extension
such as "Get cookies.txt LOCALLY" does it), run `scripts/setup-instagram.sh` on that file, then
delete the file and do not log out of that session.

An API key is not tied to a model: the agent names the model on every request, from the
`[claude]` section of `config.toml` (Claude Haiku by default, which is plenty for
understanding messages, describing what it reads and answering). The key comes from the Claude
Console, which bills separately from a claude.ai subscription. Create a key used only by this
agent, so that you can revoke it on its own.

Through MTProto the agent logs in with the bot token, never with your account: `api_id` and
`api_hash` only identify the application. Its session is the one secret the agent writes, in
`secrets/mtproto/`, the only part of `secrets/` mounted writable.

As a service, the agent's logs are in `journalctl --user -u sazed-agent -f`; after
`scripts/build.sh`, `systemctl --user restart sazed-agent` brings in the new image. It starts
when you log in; to start it with the computer, before any login, enable lingering for your user
(`loginctl enable-linger`).

Run one agent per bot. If another agent connects to the same Worker, the Worker closes this
one's connection (code 4000) and this one stops with exit code 2 instead of fighting back: two
agents would split your messages between two journals. To move the agent to another computer,
stop it here first, then copy this folder with its `data/` and `secrets/`.

## How it works

| Part | How |
|---|---|
| Connection | WebSocket client towards `sazed-bot`: backlog on start, periodic `ping`, `ack` once a message is safely in the local journal; reconnects by itself after a dropped connection or a sleep |
| Reliability | Every message goes through persistent states (received, read, saved, answered, done, or failed), and every item through its own reading state, with limited retries; one broken item never blocks the others, and questions never wait behind a long reading |
| Agent | The code saves, the model interprets: Claude says what your words are for and describes what was read, as structured data that the code checks and writes; to answer, it has read-only tools on coppermind and the recent conversation |
| Links | Short links followed; pages read with Python's HTML parser; public previews (oEmbed) for Instagram, TikTok and YouTube; yt-dlp for YouTube; only public hosts, at most 5 MB, never an address inside your home network |
| Audio and video | ffmpeg for the audio and a few frames, whisper.cpp on the processor at low priority, the first half hour at most |
| Documents | poppler for PDFs (the first page as an image, for scans); EPUB, DOCX and ODT with Python's standard library, with limits against archives that unroll beyond measure |
| Large files | MTProto (Telethon) with the bot's token, up to 2 GB, four pieces at a time |
| coppermind | A single SQLite file, shared with the journal so that saving an item and moving its message forward are one transaction: exact filters and full-text search (FTS5) |
| Meaning | One vector per item from its card (title, authors, topics, summary, note, the beginning of what was read), computed by Qwen3-Embedding-0.6B in `llama-server` on the processor, at low priority, over a private UNIX socket; compared with numpy |

Only links, metadata and extracted text are kept: the sources stay where they are, and your
files stay in the Telegram chat. Downloaded files live in a temporary folder that disappears
right after they are read. What goes to the Claude API is the text of your messages and what
was read; titles of published works go to the free catalogues; nothing else leaves your
computer.

## Make it yours

sazed is the name of the software. Your bot's name, its username and the name it introduces
itself with are yours to choose, and none of them lives in this repository: personal values
and secrets stay in files that git ignores.

The agent runs in a rootless Podman container: everything it needs lives in the image (Ubuntu
packages, plus the few that apt lacks, pinned with their hashes in `pip-requirements.txt`), and
your computer only needs Podman. Messages and texts are in Italian and English, chosen like in
`sazed-bot`.

## License

MIT: see [LICENSE](LICENSE).
