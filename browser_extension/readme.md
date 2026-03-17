**To load the extension:**
1. Unzip `harvest-queue-builder.zip`
2. Go to `chrome://extensions` → enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `harvest-queue-builder/` folder

---

**Architecture overview — 5 files:**

`manifest.json` — MV3 extension config declaring the context menu, content script injection, and storage permissions.

`background.js` (service worker) — Registers the right-click menu entry `📥 Save to Harvest Queue`. On click it messages the content script for data, deduplicates against the existing queue, persists to `chrome.storage.local`, and updates the badge counter.

`content.js` — Injected into every page. Two responsibilities: (1) listens for `contextmenu` events to capture which element was right-clicked, and (2) runs the URL + DOM extraction logic through the **site profiles**. When you right-click a *link*, `info.linkUrl` is used; when you right-click anywhere on an *item's own page*, `info.pageUrl` is the fallback — so it works both from search results and detail pages.

`popup.html` + `popup.js` — The extension popup with live filter, inline note editing (click any note text), per-item removal, copy-to-clipboard, and **Download JSON** which writes a clean `harvest_queue.json` ready to drop into `data/config/`.

**Site profiles covered and their output format:**

| Archive | `source` field | ID field(s) |
|---|---|---|
| archive.org/details/* | `archive_org` | `id` |
| polona.pl/item/\* or /preview/\* | `polona` | `id` |
| fbc.pionier.net.pl | `dlibra` | `url` |
| digitale-sammlungen.de | `dlibra` | `url` |
| gallica.bnf.fr | `dlibra` | `url` + ark ID in note |
| onb.ac.at | `dlibra` | `url` |
| deutsche-digitale-bibliothek.de | `dlibra` | `url` |
| europeana.eu | `dlibra` | `url` + Europeana path in note |
| szukajwarchiwach.gov.pl | `szwa` | `zespol` + `jednostka` from query params |
| Anything else | `unknown` | `url` |

The `dlibra` items slot directly into `m0_harvest.py`'s `fetch_dlibra()` function which reads the `url` field. For `szwa`, the query params `?zespol=X&jednostka=Y` are parsed automatically from SzWA's URL structure. Any unrecognised site gets a generic fallback — you can edit the note and source manually before downloading.