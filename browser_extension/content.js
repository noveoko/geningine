/**
 * content.js — Harvest Queue Builder
 *
 * Injected into every page.  Two jobs:
 *   1. Track the last right-clicked element so we can harvest nearby DOM
 *      metadata (title, creator, etc.) when the background worker asks.
 *   2. Render a brief toast notification confirming the save.
 *
 * ─── Site profile structure ───────────────────────────────────────────────
 * Each profile must implement:
 *
 *   buildEntry(targetUrl, clickedElement) → Object | null
 *     Returns a harvest_queue.json entry, or null if the URL doesn't match.
 *     The returned object must include a `source` field and whichever ID
 *     fields that source needs (id / url / zespol+jednostka).
 *
 * The `clickedElement` is the DOM node the user right-clicked — use it to
 * walk up the tree and find richer metadata than the URL alone provides.
 */

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// 1.  Track the right-clicked element
// ─────────────────────────────────────────────────────────────────────────────

let _lastRightClicked = null;

document.addEventListener('contextmenu', (e) => {
  _lastRightClicked = e.target;
}, /* capture */ true);


// ─────────────────────────────────────────────────────────────────────────────
// 2.  DOM helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Walk up from `el`, try each CSS selector in order, return first match's
 * text or title attribute.  Falls back to the page's <h1>.
 */
function nearestText(el, containerSelectors, titleSelectors) {
  if (el) {
    for (const cs of containerSelectors) {
      const container = el.closest(cs);
      if (!container) continue;
      for (const ts of titleSelectors) {
        const found = container.querySelector(ts);
        if (found) {
          return (found.getAttribute('title') || found.textContent || '').trim();
        }
      }
    }
  }
  // Last resort: page heading
  const h1 = document.querySelector('h1');
  return h1 ? h1.textContent.trim() : document.title.trim();
}

/** Clean up whitespace and truncate long strings for the `note` field. */
function sanitise(str, maxLen = 120) {
  return (str || '').replace(/\s+/g, ' ').trim().slice(0, maxLen);
}


// ─────────────────────────────────────────────────────────────────────────────
// 3.  Site profiles
// ─────────────────────────────────────────────────────────────────────────────

const PROFILES = [

  // ── Archive.org ────────────────────────────────────────────────────────────
  {
    name: 'archive_org',
    // Matches item detail pages AND search-result links
    // e.g.  https://archive.org/details/sanfranciscotele1904paci_0
    test: (url) => /archive\.org\/(details|download)\//.test(url),

    buildEntry(url, el) {
      const m = url.match(/archive\.org\/(?:details|download)\/([^/?#\s]+)/);
      if (!m) return null;
      const id = m[1];
      const note = sanitise(nearestText(el,
        // Container selectors (broad → narrow)
        ['[data-id]', '.item-ia', '.tile-details', 'article', '.result'],
        // Title selectors inside the container
        ['h3[title]', 'h2[title]', 'h3', 'h2', '[title]', '.item-title'],
      ));
      return { source: 'archive_org', id, note };
    },
  },

  // ── Polona.pl ──────────────────────────────────────────────────────────────
  {
    name: 'polona',
    // e.g.  https://polona.pl/item/xxx  or  /preview/xxx  or  /pl/xxx
    test: (url) => /polona\.pl\/(item|preview|pl)\//.test(url),

    buildEntry(url, el) {
      const m = url.match(/polona\.pl\/(?:item|preview|pl)\/([^/?#\s]+)/);
      if (!m) return null;
      const id = m[1];
      const note = sanitise(nearestText(el,
        ['.search-result', '.item', 'article', '[class*="card"]', '[class*="result"]'],
        ['h2[title]', 'h3[title]', 'h2', 'h3', '.title', '[title]'],
      ));
      return { source: 'polona', id, note };
    },
  },

  // ── FBC / Pionier (dLibra consortium portal) ───────────────────────────────
  {
    name: 'fbc',
    // e.g.  https://fbc.pionier.net.pl/id/oai:xxx   or  /details/xxx
    test: (url) => /fbc\.pionier\.net\.pl/.test(url),

    buildEntry(url, el) {
      // dLibra source in m0 needs `url`, not `id`
      const cleanUrl = url.split('?')[0].split('#')[0];
      const note = sanitise(nearestText(el,
        ['.result', 'article', 'tr', 'li', '[class*="item"]'],
        ['h2', 'h3', 'a', 'td', '.title'],
      ));
      return { source: 'dlibra', url: cleanUrl, note };
    },
  },

  // ── Any dLibra-powered site (WBC Poznań, etc.) ─────────────────────────────
  // A dLibra publication URL typically contains /publication/ or /Content/
  {
    name: 'dlibra_generic',
    test: (url) => /\/(publication|Content|dlibra)\//.test(url),

    buildEntry(url, el) {
      const cleanUrl = url.split('?')[0].split('#')[0];
      const note = sanitise(nearestText(el,
        ['.result', 'article', 'tr', 'li'],
        ['h2', 'h3', '.title', 'td'],
      ));
      return { source: 'dlibra', url: cleanUrl, note };
    },
  },

  // ── Bayerische Staatsbibliothek — digitale-sammlungen.de ──────────────────
  {
    name: 'bsb',
    test: (url) => /digitale-sammlungen\.de/.test(url),

    buildEntry(url, el) {
      // URL forms:
      //   https://www.digitale-sammlungen.de/view/bsb10000001
      //   https://www.digitale-sammlungen.de/en/view/bsb10000001
      const m = url.match(/digitale-sammlungen\.de\/(?:\w{2}\/)?view\/([^/?#]+)/);
      const note = sanitise(nearestText(el,
        ['article', '.result', '[class*="teaser"]', '[class*="item"]'],
        ['h2', 'h3', '.title'],
      ));
      if (m) {
        // No native m0 fetcher → store as dlibra with URL
        return { source: 'dlibra', url, note };
      }
      return { source: 'dlibra', url, note };
    },
  },

  // ── Gallica (BnF) ──────────────────────────────────────────────────────────
  {
    name: 'gallica',
    // e.g.  https://gallica.bnf.fr/ark:/12148/bpt6k9604118j
    test: (url) => /gallica\.bnf\.fr/.test(url),

    buildEntry(url, el) {
      const m = url.match(/gallica\.bnf\.fr\/ark:\/12148\/([^/?#\s]+)/);
      const arkId = m ? m[1] : null;
      const note = sanitise(nearestText(el,
        ['.result-item', '.item', 'article', '[class*="result"]'],
        ['h2', 'h3', '.title', '[title]'],
      ));
      // No native m0 fetcher — store URL; add arkId to note for reference
      return { source: 'dlibra', url, note: arkId ? `ark:${arkId} — ${note}` : note };
    },
  },

  // ── Austrian National Library (ONB) ────────────────────────────────────────
  {
    name: 'onb',
    test: (url) => /onb\.ac\.at/.test(url),

    buildEntry(url, el) {
      const note = sanitise(nearestText(el,
        ['article', '[class*="item"]', '[class*="result"]', '.card'],
        ['h2', 'h3', '.title'],
      ));
      return { source: 'dlibra', url, note };
    },
  },

  // ── Deutsche Digitale Bibliothek ────────────────────────────────────────────
  {
    name: 'ddb',
    // e.g.  https://www.deutsche-digitale-bibliothek.de/item/XXXX
    test: (url) => /deutsche-digitale-bibliothek\.de/.test(url),

    buildEntry(url, el) {
      const m = url.match(/deutsche-digitale-bibliothek\.de\/item\/([^/?#\s]+)/);
      const note = sanitise(nearestText(el,
        ['article', '[class*="teaser"]', '[class*="result"]', '.card'],
        ['h2', 'h3', '.title'],
      ));
      // Store as dlibra-style URL entry (no native m0 fetcher)
      return { source: 'dlibra', url, note };
    },
  },

  // ── Europeana ───────────────────────────────────────────────────────────────
  {
    name: 'europeana',
    // e.g.  https://www.europeana.eu/en/item/2048128/xxx
    test: (url) => /europeana\.eu/.test(url),

    buildEntry(url, el) {
      // Europeana item path: /en/item/{collection}/{itemId}
      const m = url.match(/europeana\.eu\/[^/]+\/item\/([^?#\s]+)/);
      const euroId = m ? m[1] : null;
      const note = sanitise(nearestText(el,
        ['[class*="card"]', 'article', '[class*="item"]', '[class*="result"]'],
        ['h2', 'h3', '.title', '[title]'],
      ));
      return { source: 'dlibra', url, note: euroId ? `europeana:${euroId} — ${note}` : note };
    },
  },

  // ── Szukaj w Archiwach (SzWA) ───────────────────────────────────────────────
  {
    name: 'szwa',
    test: (url) => /szukajwarchiwach\.gov\.pl/.test(url),

    buildEntry(url, el) {
      try {
        const u = new URL(url);
        const zespol    = u.searchParams.get('zespol')    || '';
        const jednostka = u.searchParams.get('jednostka') || '';
        const note = sanitise(nearestText(el,
          ['tr', 'article', '.result', 'li'],
          ['td', 'h2', 'h3', 'a'],
        ));
        if (zespol && jednostka) {
          return { source: 'szwa', zespol, jednostka, note };
        }
        // URL without query params — store as generic with a reminder note
        return {
          source: 'szwa',
          url,
          note: note || '⚠ Add zespol + jednostka manually',
        };
      } catch {
        return { source: 'szwa', url, note: '' };
      }
    },
  },

]; // end PROFILES


// ─────────────────────────────────────────────────────────────────────────────
// 4.  Entry-point: resolve a URL against all profiles
// ─────────────────────────────────────────────────────────────────────────────

function extractItemData(targetUrl, clickedElement) {
  // Normalise — some hrefs are relative
  let resolvedUrl = targetUrl;
  try {
    resolvedUrl = new URL(targetUrl, window.location.href).href;
  } catch { /* keep as-is */ }

  for (const profile of PROFILES) {
    try {
      if (profile.test(resolvedUrl)) {
        const entry = profile.buildEntry(resolvedUrl, clickedElement);
        if (entry) return entry;
      }
    } catch (err) {
      console.warn(`[HQB] Profile "${profile.name}" threw:`, err);
    }
  }

  // ── Generic fallback ───────────────────────────────────────────────────────
  // No profile matched.  Store URL + whatever title we can find, marking the
  // source as 'unknown' so the user knows it needs review.
  const title = sanitise(
    document.querySelector('h1')?.textContent || document.title
  );
  return { source: 'unknown', url: resolvedUrl, note: title };
}


// ─────────────────────────────────────────────────────────────────────────────
// 5.  Message listener (called by background.js)
// ─────────────────────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {

  // ── Extract item data ──────────────────────────────────────────────────────
  if (msg.action === 'extractItemData') {
    const entry = extractItemData(msg.targetUrl, _lastRightClicked);
    sendResponse(entry);
    return true; // keep message channel open for async
  }

  // ── Show toast notification ────────────────────────────────────────────────
  if (msg.action === 'showToast') {
    showToast(msg.message, msg.type || 'saved');
    return;
  }
});


// ─────────────────────────────────────────────────────────────────────────────
// 6.  Toast UI
// ─────────────────────────────────────────────────────────────────────────────

let _toastEl = null;
let _toastTimer = null;

function showToast(message, type) {
  // Create on first use
  if (!_toastEl) {
    _toastEl = document.createElement('div');
    _toastEl.id = '__hqb-toast__';
    // Scoped inline styles — avoids any page CSS conflict
    Object.assign(_toastEl.style, {
      position:       'fixed',
      bottom:         '24px',
      right:          '24px',
      zIndex:         '2147483647',
      padding:        '12px 18px',
      borderRadius:   '6px',
      fontFamily:     '"IBM Plex Mono", "Courier New", monospace',
      fontSize:       '13px',
      fontWeight:     '500',
      lineHeight:     '1.4',
      maxWidth:       '340px',
      boxShadow:      '0 4px 20px rgba(0,0,0,0.35)',
      transition:     'opacity 0.25s ease, transform 0.25s ease',
      pointerEvents:  'none',
      userSelect:     'none',
    });
    document.body.appendChild(_toastEl);
  }

  // Colour scheme per type
  const schemes = {
    saved:     { bg: '#1a2e1a', border: '#4caf50', color: '#a5d6a7' },
    fallback:  { bg: '#2e2a1a', border: '#c8a96e', color: '#ffe082' },
    duplicate: { bg: '#1e1e2e', border: '#7986cb', color: '#9fa8da' },
    error:     { bg: '#2e1a1a', border: '#ef5350', color: '#ef9a9a' },
  };
  const s = schemes[type] || schemes.saved;

  Object.assign(_toastEl.style, {
    background:   s.bg,
    border:       `1px solid ${s.border}`,
    color:        s.color,
    opacity:      '0',
    transform:    'translateY(8px)',
  });
  _toastEl.textContent = message;

  // Animate in
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      _toastEl.style.opacity   = '1';
      _toastEl.style.transform = 'translateY(0)';
    });
  });

  // Auto-dismiss
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    _toastEl.style.opacity   = '0';
    _toastEl.style.transform = 'translateY(8px)';
  }, 3000);
}
