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
  chrome.contextMenus.create({
    id:       'hqb-save',
    title:    '📥  Save to Harvest Queue',
    contexts: ['link', 'page'],
  });

  // Second entry: save the item AND scan the page for fuzzy-similar titles
  chrome.contextMenus.create({
    id:       'hqb-save-similar',
    title:    '🔍  Save + Find Similar (≥75% match)',
    contexts: ['link', 'page'],
  });

  chrome.storage.local.get('harvestQueue', ({ harvestQueue }) => {
    if (!harvestQueue) {
      chrome.storage.local.set({ harvestQueue: [] });
    }
  });
});


// ─── Context menu click handler ───────────────────────────────────────────────

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== 'hqb-save' && info.menuItemId !== 'hqb-save-similar') return;

  const findSimilar = (info.menuItemId === 'hqb-save-similar');
  const targetUrl   = info.linkUrl || info.pageUrl;

  // ── Step 1: extract and save the clicked item ──────────────────────────────
  let entry    = null;
  let feedback = 'saved';

  try {
    entry = await chrome.tabs.sendMessage(tab.id, {
      action: 'extractItemData', targetUrl, pageUrl: info.pageUrl,
    });
  } catch (_) {
    entry    = buildFallbackEntry(targetUrl);
    feedback = 'fallback';
  }
  if (!entry) {
    entry    = buildFallbackEntry(targetUrl);
    feedback = 'fallback';
  }

  let { harvestQueue = [] } = await chrome.storage.local.get('harvestQueue');

  const isDup = harvestQueue.some(e => isSameItem(e, entry));
  if (!isDup) {
    harvestQueue.push(entry);
  } else {
    feedback = 'duplicate';
  }

  // ── Step 2 (optional): fuzzy-scan the page for similar titles ─────────────
  let similarAdded = 0;

  if (findSimilar && feedback !== 'duplicate') {
    // Use the item's note as the reference title (our best title proxy).
    // Fall back to id or the last path segment of the URL.
    const refTitle = entry.note
      || entry.id
      || (entry.url || '').split('/').pop()
      || '';

    if (refTitle) {
      let similarResults = [];
      try {
        similarResults = await chrome.tabs.sendMessage(tab.id, {
          action: 'findSimilar',
          referenceTitle: refTitle,
          threshold: 0.55,
        });
      } catch (_) { /* content script unavailable */ }

      for (const { entry: sim } of (similarResults || [])) {
        if (isSameItem(sim, entry)) continue;
        if (harvestQueue.some(e => isSameItem(e, sim))) continue;
        harvestQueue.push(sim);
        similarAdded++;
      }
    }
  }

  await chrome.storage.local.set({ harvestQueue });
  updateBadge(harvestQueue.length);

  // ── Toast ──────────────────────────────────────────────────────────────────
  let toastMsg, toastType;
  if (feedback === 'duplicate') {
    toastMsg  = '🔁  Already in queue — skipped';
    toastType = 'duplicate';
  } else if (findSimilar && similarAdded > 0) {
    toastMsg  = `✅  Saved + ${similarAdded} similar item${similarAdded !== 1 ? 's' : ''} added (${harvestQueue.length} total)`;
    toastType = 'similar';
  } else if (findSimilar && similarAdded === 0) {
    toastMsg  = `✅  Saved — no other similar items found on this page (${harvestQueue.length} total)`;
    toastType = 'saved';
  } else if (feedback === 'fallback') {
    toastMsg  = `⚠️  Saved with limited metadata (${harvestQueue.length} items)`;
    toastType = 'fallback';
  } else {
    toastMsg  = `✅  Saved to queue (${harvestQueue.length} items)`;
    toastType = 'saved';
  }

  try {
    await chrome.tabs.sendMessage(tab.id, {
      action: 'showToast', message: toastMsg, type: toastType,
    });
  } catch (_) { /* tab may not accept messages */ }
});

// ─── Internal message listener (popup → background) ──────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === '_syncBadge') {
    chrome.storage.local.get('harvestQueue', ({ harvestQueue = [] }) => {
      updateBadge(harvestQueue.length);
    });
  }
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
