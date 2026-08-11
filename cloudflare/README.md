# Cloudflare 08:50 FedWatch push

This Worker runs every day at `00:50 UTC` (`08:50 Asia/Shanghai`), scrapes the
CME FedWatch QuikStrike iframe with Cloudflare Browser Run, writes the daily JSON
and history CSV to GitHub, and sends the result through ServerChan. A failed scrape is retried up to three times inside the same
scheduled invocation, so successful runs produce only one notification.

## Deploy

```bash
cd cloudflare
npm install
npx wrangler login
npx wrangler secret put SERVERCHAN_SENDKEY
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put MANUAL_TRIGGER_TOKEN
npm run check
npm run deploy
```

`MANUAL_TRIGGER_TOKEN` should be a newly generated random value. It protects the
manual test endpoint and is not the ServerChan key.

## Verify before enabling a real push

After deployment, first run a scrape without sending a notification:

```bash
curl -H "Authorization: Bearer YOUR_MANUAL_TRIGGER_TOKEN" \
  "https://cme-fedwatch-push.YOUR_SUBDOMAIN.workers.dev/"
```

When the returned JSON contains `"ok":true`, test ServerChan once:

```bash
curl -H "Authorization: Bearer YOUR_MANUAL_TRIGGER_TOKEN" \
  "https://cme-fedwatch-push.YOUR_SUBDOMAIN.workers.dev/?push=1"
```

Health check (no authentication required):

```bash
curl "https://cme-fedwatch-push.YOUR_SUBDOMAIN.workers.dev/health"
```

Follow production logs with `npm run logs`. Cron expressions use UTC; the
configured `50 0 * * *` is 08:50 in China year-round.

## Secrets

- `SERVERCHAN_SENDKEY`: ServerChan SendKey, stored only as a Worker secret.
- `GITHUB_TOKEN`: fine-grained GitHub token with **Contents: Read and write**
  permission for `zuowood1234/cme-fedwatch-tracker`.
- `MANUAL_TRIGGER_TOKEN`: random bearer token used only by the manual endpoint.

Do not commit either value to Git.
