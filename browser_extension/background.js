/**
 * background.js — Harvest Queue Builder (MV3 Service Worker)
 *
 * Responsibilities:
 *   1. Register the right-click context menu.
 *   2. On menu click, ask the content script for item data.
 *   3. Deduplicate and persist items via chrome.storage.local.
 *   4. Keep the popup badge in sync with queue length.
 */

// ─── Context menu setup ───────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
  // Show menu when right-clicking a hyperlink OR anywhere on the page
  // (covers both search-results pages and individual item pages)
  chrome.contextMenus.create({
    id:       'hqb-save',
    title:    '📥  Save to Harvest Queue',
    contexts: ['link', 'page'],
  });

  // Initialise storage if first install
  chrome.storage.local.get('harvestQueue', ({ harvestQueue }) => {
    if (!harvestQueue) {
      chrome.storage.local.set({ harvestQueue: [] });
    }
  });
});


// ─── Context menu click handler ───────────────────────────────────────────────

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== 'hqb-save') return;

  // Prefer the link's own URL; fall back to the current page URL.
  // This lets users:
  //   A) Right-click a search-result link  → captures that item's URL
  //   B) Right-click anywhere on an item's own detail page → captures page URL
  const targetUrl = info.linkUrl || info.pageUrl;

  let entry = null;
  let feedback = 'saved';

  try {
    // Ask the content script to: (a) resolve the URL against site profiles,
    // and (b) harvest nearby DOM metadata (title, etc.).
    entry = await chrome.tabs.sendMessage(tab.id, {
      action:    'extractItemData',
      targetUrl: targetUrl,
      pageUrl:   info.pageUrl,
    });
  } catch (err) {
    // Content script unavailable (e.g. chrome:// pages, PDFs).
    // Build a minimal fallback entry ourselves from the URL alone.
    entry = buildFallbackEntry(targetUrl);
    feedback = 'fallback';
  }

  if (!entry) {
    // Content script returned null — unrecognised URL pattern.
    entry = buildFallbackEntry(targetUrl);
    feedback = 'fallback';
  }

  // ── Persist ────────────────────────────────────────────────────────────────
  const { harvestQueue = [] } = await chrome.storage.local.get('harvestQueue');

  // Deduplication: same source + same id/url means it's already in the queue.
  const isDup = harvestQueue.some(existing => isSameItem(existing, entry));

  if (!isDup) {
    harvestQueue.push(entry);
    await chrome.storage.local.set({ harvestQueue });
  } else {
    feedback = 'duplicate';
  }

  // ── Badge ──────────────────────────────────────────────────────────────────
  updateBadge(harvestQueue.length);

  // ── Toast in the page ─────────────────────────────────────────────────────
  const messages = {
    saved:     `✅  Saved to queue (${harvestQueue.length} items)`,
    fallback:  `⚠️  Saved with limited metadata (${harvestQueue.length} items)`,
    duplicate: '🔁  Already in queue — skipped',
  };
  try {
    await chrome.tabs.sendMessage(tab.id, {
      action:  'showToast',
      message: messages[feedback],
      type:    feedback,
    });
  } catch (_) { /* tab may not accept messages */ }
});


// ─── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Build a minimal entry purely from the URL when the content script is
 * unreachable.  Used for PDFs, chrome:// pages, etc.
 */
function buildFallbackEntry(url) {
  try {
    const u = new URL(url);
    return {
      source: 'unknown',
      url:    url,
      note:   u.hostname + u.pathname,
    };
  } catch {
    return { source: 'unknown', url, note: '' };
  }
}

/**
 * Two queue entries are considered the same if they share:
 *   - source system AND
 *   - item identifier (id for archive_org/polona, url for dlibra-style,
 *     or zespol+jednostka for szwa)
 */
function isSameItem(a, b) {
  if (a.source !== b.source) return false;
  if (a.id  && b.id)  return a.id  === b.id;
  if (a.url && b.url) return a.url === b.url;
  if (a.zespol && b.zespol) {
    return a.zespol === b.zespol && a.jednostka === b.jednostka;
  }
  return false;
}

/**
 * Show item count on the extension badge.
 * Green = has items, grey = empty.
 */
function updateBadge(count) {
  chrome.action.setBadgeText({ text: count > 0 ? String(count) : '' });
  chrome.action.setBadgeBackgroundColor({
    color: count > 0 ? '#c8a96e' : '#888888',
  });
}

// Keep badge correct after browser restarts (service workers are ephemeral)
chrome.storage.local.get('harvestQueue', ({ harvestQueue = [] }) => {
  updateBadge(harvestQueue.length);
});
