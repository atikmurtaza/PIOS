# PIOS Browser Sensor

PIOS can see that `msedge.exe` had focus for 191 minutes, but not what you were
doing. This extension tells it: for each page you finish looking at, it sends
the URL, the page title, and how long it was in front — to `127.0.0.1` only.

It never sends page contents, never touches incognito windows, and skips any
domain on the blocklist it fetches from PIOS (banking, password managers,
login pages — editable in PIOS → Privacy).

## Install (Edge)

1. Start PIOS, open <http://127.0.0.1:8321>, go to **Privacy** and copy the
   browser token.
2. Go to `edge://extensions`.
3. Turn on **Developer mode** (left sidebar).
4. Click **Load unpacked** and select this `extension` folder.
5. Click **Details** on "PIOS Browser Sensor" → **Extension options**.
6. Paste the token, leave the port at 8321, click **Save & test connection**.
   You should see "Connected".

## Install (Chrome)

Same, at `chrome://extensions`: **Developer mode** (top-right toggle) →
**Load unpacked** → select this folder → **Details** → **Extension options** →
paste token → **Save & test connection**.

## Checking it works

Browse a couple of sites, switch tabs (a visit is only recorded once you leave
the page), then in PIOS → **Today** you should see events whose app is the
hostname, e.g. `github.com`. Visits shorter than 2 seconds are ignored.

If PIOS isn't running the extension stays quiet and retries once a minute;
at most 200 pending visits are held in memory and the oldest are dropped.

## Turning it off

Uncheck **Browser sensor** in PIOS → Privacy → Save (the extension picks this
up within 30 minutes and PIOS drops anything sent meanwhile), or just remove
the extension.
