# Fixel free trial server

A Cloudflare Worker that gives anyone without a Gemini key **3 free questions a day**. The owner's Gemini
key lives here as a secret, never in the app or on GitHub. After 3 questions Fixel tells people to add
their own key with `/api gemini` (free at aistudio.google.com/apikey).

Limits per UTC day, set in `wrangler.toml`: `DAILY_PER_INSTALL` (3), `DAILY_PER_IP` (30, since a school
shares one IP) and `DAILY_TOTAL` (200, which keeps the owner's own free quota safe). Only Gemini
free-tier models are called, whatever a client asks for. A question is counted only once Gemini starts
answering, so errors never use one up.

## Deploy (once)

```sh
cd server/trial
npx wrangler@4 login                          # opens the browser
npx wrangler@4 kv namespace create TRIAL      # paste the id it prints into wrangler.toml
npx wrangler@4 secret put GEMINI_API_KEY      # a key from a Google project WITHOUT billing
npx wrangler@4 deploy                         # prints https://fixel-trial.<you>.workers.dev
```

Put that URL in `TRIAL_URL` in `fixel/config.py`.

## Test

```sh
npm test    # Node's built-in runner, nothing to install
```
