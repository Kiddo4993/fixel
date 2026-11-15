# Changelog

## Unreleased

**Free trial**
- No Gemini key yet? Fixel answers 3 questions a day through its free trial server (server/trial, a Cloudflare Worker that holds the key). After that it asks you to add your own free key with `/api gemini`. With your own key the trial is never used.

**Free by default**
- New `free_only` setting, on by default: Fixel uses only Gemini's free-tier models and refuses Claude, OpenAI and paid-only Gemini models, so it never spends API credits. Set `free_only = false` to allow them.
- Gemini is the default model. The backup models are now free-tier ones (`gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-2.5-flash`); `gemini-3.5-flash` is no longer free.
- A rate limit moves straight on to the next free model instead of retrying the same one, a model whose daily quota is used up is skipped for the rest of the session, and the error says how long to wait or that the daily limit resets at midnight Pacific.
- A retired backup model no longer stops the others from answering.

**Fixes**
- Screenshots are always saved as PNG. With macOS set to save JPG or HEIC screenshots, the first shot used to crash the hotkey for the rest of the session.
- One failed screenshot no longer turns off the hotkey.
- `/open` no longer crashes on a case whose question contains something like `[/usr/bin]`.
- `/cases`, `/open` and `/worked` report a damaged case file instead of quitting Fixel.
- `/canvas` keeps the plans from batches that finished before a later batch failed, so it doesn't pay to plan them again.
- A calendar feed link pasted with `http://` is fetched over https, so the secret link isn't sent unencrypted.
- A typo in `config.toml` gives a one-line message instead of a traceback.

**Security**
- The playground server only answers requests addressed to 127.0.0.1 or localhost, which blocks DNS-rebinding pages.
- The test suite can't read or overwrite your real Keychain keys or reach the network.

## 0.2.0 (2026-11-03)

The first public release.

**Screenshots and answers**
- Double-tap Right Option anywhere, drag over what you're stuck on, and read the answer in your terminal.
- Each screenshot is sorted into a mode (error, math, writing, email, webpage, …) and answered in the format that fits: a command to run, numbered steps, essay feedback, a draft reply, or an HTML file that recreates the page.
- Fixel remembers what worked: when a solved error comes back, the fix that worked last time goes into the prompt.
- Learn mode gives hints instead of answers.
- Bring your own Claude, Gemini or OpenAI key, kept in the macOS Keychain.

**Playground**
- `/ide [language]` opens an IDE for Python, Java, JavaScript or C++ in its own Terminal window. Find, go to line, completion and auto-indent are built in.
- `/ide here` (or `/code`) keeps it in the current window. `/ide web` opens a Monaco editor in the browser.
- Send your code back to Fixel to ask about it.

**Canvas and Notion**
- `/canvas` lists what's due, with a suggested priority, hours, start-by date and tip for each assignment. It works with a Canvas API token or a calendar feed.
- It can keep a Notion database in sync without overwriting the Status you set.

**Claude Code**
- Fixel ships as a Claude Code plugin: `/plugin install fixel --marketplace Kiddo4993/fixel`.
- New one-shot commands for scripts and for Claude: `fixel shot`, `fixel canvas --json`, `fixel cases <words> --json`.
