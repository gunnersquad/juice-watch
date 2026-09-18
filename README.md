# Juice Watch bot

Watches fragrance stores twice an hour and alerts your phone when a **popular designer or niche fragrance** has a likely price error or a really good price. You don't enter anything: it recognizes the brands and scents on its own. It runs for free on GitHub Actions, so your phone and computer can stay off.

## What you'll get

**Possible price error** (high-priority alert), when any of these happen:
- a popular fragrance is 50% or more below its recent price at that store,
- it's 60% or more below the typical price for the same fragrance and size at other stores,
- it's listed at $1 or less.

**Really good price** (normal alert), sent under one of three headings:
- **Great price:** 30% or more below the typical price for the same bottle at at least two other stores. The alert also names the next-cheapest store so you can compare.
- **Big price drop:** 30% or more below its recent price at that store.
- **Lowest price in a while:** the lowest price the bot has seen for that listing in 90 days, and at least 15% below its recent price. This only starts after the bot has watched the listing for 14 days.

Each alert shows the product, the price, every reason it was flagged, the brand, whether it's one of that brand's best-known scents, and a link. Errors and famous scents are sent first.

**How the comparisons work:**
- "Recent price" is the highest price the bot has seen at that store in the last 14 days. If a lower price holds for 14 days, it becomes the new normal.
- "Typical price" is the middle price across the other stores.
- Cross-store comparisons only work for the best-known scents in `popular.json`, and matching is strict. Two listings count as the same bottle only if their brand, full product name (so Angel and Angel Fantasm are different), concentration, size, gender, and tester status all match.
- If a title leaves out the concentration or gender, the bot won't guess, so some real matches get skipped. That's on purpose: missing a comparison is better than a false alert.
- Deodorants, candles, lotions, and other non-perfume items are ignored entirely. Gift sets and travel sprays are left out of cross-store comparisons.
- How many matches you get depends on how many stores carry the same bottles. The log shows the count each run.

## How it recognizes fragrances

`popular.json` lists about 30 designer houses (Dior, Chanel, YSL, Armani, Tom Ford, and more) and about 30 niche houses (Creed, Parfums de Marly, MFK, Xerjoff, Initio, Kilian, Amouage, and more), along with each brand's best-known scents.

- The bot checks each product's brand field first, then its title.
- Listings that say "inspired by," "dupe," or similar are ignored, so clones don't pass as the real thing.
- Decants, samples, travel sizes, and bottles of 15 ml (0.5 oz) or less are skipped.
- Testers are included and labeled as testers.
- Middle Eastern houses (Lattafa, Armaf, Afnan, and others) are in the file but turned off. To include them, add `"middle_eastern"` to `tiers_enabled` at the top of `popular.json`.
- To add a brand, copy an existing brand line in `popular.json` and change it.

## Which stores get scanned

Automatic scanning needs a store that publishes its full product list. Shopify stores do.

- **Scanned automatically:** Arvella, AuraFragrance, FragFlex, Beauty House, and Perfumania.
- **Detected on their first run:** Olfactory Factory, Venba Fragrance, Fragrance Lord, Fragrance Nevaeh, Emnt Scents, and Mystic Perfume. Any that offer the product list get scanned, and the rest are skipped. The log shows the result, and the check repeats weekly.
- **Not scanned automatically:** FragranceNet, FragranceX, Jomashop, MaxAroma, and Notino. Scanning them would mean loading thousands of product pages every run, which isn't polite and gets bots blocked. You can still watch specific products at these stores (see "Optional watchlist").

The first run only records prices, so alerts begin on the second run, about 30 minutes later. On the first run that compares stores, deals that already exist are saved quietly instead of sent all at once, so only new deals reach your phone after that. Price errors are still sent.

## Setup (about 15 minutes, works on a phone)

### 1. Get the alert app

1. Install **ntfy** from the App Store or Google Play.
2. Tap **+** and subscribe to a long, random topic name, like `juice-watch-k39xq7vm2`. Anyone who knows the name can read your alerts.

### 2. Create the GitHub repository

1. Sign up at github.com if you don't have an account.
2. Tap **+** then **New repository**. Name it `juice-watch` and set it to **Public**.
   - Public repos get GitHub Actions for free with no minute limit.
   - Private repos get 2,000 free minutes a month, which isn't enough for checks every 30 minutes. If you go private, run every 2 hours instead (see "Settings").
3. Tick **Add a README file** and tap **Create repository**.

### 3. Add the files

For each file, tap **Add file** then **Create new file**, type the name exactly as shown, paste the file's contents, and tap **Commit changes**.

- `juice_watch.py`
- `watchlist.json`
- `popular.json`
- `.github/workflows/juice-watch.yml` (typing the slashes creates the folders)

### 4. Add your ntfy topic as a secret

1. In the repo, go to **Settings**, then **Secrets and variables**, then **Actions**.
2. Tap **New repository secret**.
3. Set the name to `NTFY_TOPIC` and the value to your topic name.

For Discord, also add `DISCORD_WEBHOOK_URL` with a webhook link from your Discord channel's settings.

### 5. Test it

1. Open the **Actions** tab and enable workflows if GitHub asks.
2. Tap **Juice Watch**, then **Run workflow**. Choose `test` to get a test alert.
3. Run it again with `normal` to record the first set of prices.

From then on it runs automatically at 7 and 37 minutes past each hour. GitHub sometimes starts scheduled runs a few minutes late.

## Settings

The `rules` section of `watchlist.json`:

| Setting | Default | What it does |
|---|---|---|
| `popular_only` | true | Only alert on brands in `popular.json`. Set to false to alert on anything (store checks only) |
| `famous_only` | false | Only alert on the best-known scents listed under each brand |
| `error_drop_percent` | 50 | Drop from recent price that counts as a possible error |
| `error_vs_market_percent` | 60 | How far below other stores counts as a possible error |
| `compare_at_percent` | null (off) | Flags prices far below the store's own list price. Off by default because some stores show inflated list prices. Set a number like 90 to turn it on |
| `absurd_price` | 1.0 | Anything at or below this price is flagged |
| `big_drop_percent` | 20 | Drop from recent price that sends a "big price drop" alert |
| `good_vs_market_percent` | 20 | How far below other stores sends a "great price" alert |
| `market_min_other_stores` | 2 | How many other stores must carry the same bottle before comparing |
| `record_low_days` | 90 | Window for "lowest price in a while" |
| `record_low_min_drop_percent` | 10 | Minimum drop needed for a "lowest price" alert |
| `record_low_min_history_days` | 14 | How long the bot watches a listing before sending "lowest price" alerts |
| `reference_days` | 14 | How long a higher price counts as "recent" |
| `ignore_samples` | true | Skip decants, samples, and small sizes |
| `max_alerts_per_run` | 10 | Caps alerts per run. Extras are listed in the log |
| `parallel_stores` | 6 | How many stores are scanned at the same time |

If you get too many alerts, raise `big_drop_percent` and `good_vs_market_percent`, or turn on `famous_only`. If you only want errors, set both of those to 100.

To change how often it runs, edit the `cron` line in `.github/workflows/juice-watch.yml`:
- `"7,37 * * * *"` runs every 30 minutes.
- `"7 * * * *"` runs hourly.
- `"7 */2 * * *"` runs every 2 hours.

## Adding stores

Add an entry to `stores` in `watchlist.json`:

```json
{"name": "Store Name", "base_url": "https://www.store.com", "platform": "auto", "scan_catalog": true}
```

There's no limit on the number of stores. Up to 6 stores are scanned at the same time (`parallel_stores`), each still with its own 5–8 second gap between requests, so adding a store adds little to the run time unless it has a very large catalog. When a new store joins cross-store comparisons, price gaps that already exist with it are saved quietly instead of sent all at once.

## Optional watchlist

This is for specific products, including ones at FragranceNet, FragranceX, and other stores that can't be scanned automatically. Add entries to `watchlist` in `watchlist.json`:

```json
{"name": "Bleu de Chanel EDP 3.4 oz", "store": "FragranceNet", "url": "https://www.fragrancenet.com/the-product-link", "variant": "", "usual_price": 120.00}
```

You'll get an alert when the product is 60% or more below `usual_price` (you can change that with `watchlist_percent`).

- On pages that list several sizes, the price the bot reads may belong to the cheapest size.
- Notino may show prices in euros. Use the same currency for `usual_price`.
- `variant` only applies to Shopify stores. Put a size word in it, like `"3.4"`.

## How it stays polite

- It spaces requests to each store 5–8 seconds apart and checks one store at a time.
- It follows each store's robots.txt and identifies itself as a bot.
- If a store says "slow down" (HTTP 429), it waits once, then skips that store until the next run.
- If a store refuses access (HTTP 403), it stops asking for the rest of the run.

## Things to know

- **Price history** is kept in GitHub's Actions cache. If it's cleared, the next run re-learns prices without sending alerts.
- **Pauses:** GitHub pauses scheduled workflows in public repos after 60 days with no repository activity and emails you first. You can re-enable the workflow from the Actions tab.
- **Checking a run:** open **Actions**, tap a run, then tap **check** to see how many listings were scanned, how many came from popular brands, and how many famous fragrances were found at two or more stores.
- **Before you buy:** always confirm the price on the product page. Stores can cancel orders placed at mistaken prices.
