/**
 * content.js — Harvest Queue Builder
 *
 * Injected into every page.  Three jobs:
 *   1. Track the last right-clicked element so we can harvest nearby DOM
 *      metadata (title, creator, etc.) when the background worker asks.
 *   2. Render toast notifications confirming saves.
 *   3. Fuzzy-match page items against a reference title on demand.
 *
 * ─── Site profile structure ───────────────────────────────────────────────
 * Each profile must implement:
 *
 *   buildEntry(targetUrl, clickedElement) → Object | null
 *     Returns a harvest_queue.json entry, or null if the URL doesn't match.
 *     The returned object must include a `source` field and whichever ID
 *     fields that source needs (id / url / zespol+jednostka).
 *
 *   enumerateItems(doc) → Array<{ el, url, title }>
 *     Scans the page for ALL item cards/rows visible in the current DOM and
 *     returns their link element, resolved URL, and best title string.
 *     Used by findSimilarOnPage() to build the candidate pool.
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
// 3.  Fuzzy matching engine
// ─────────────────────────────────────────────────────────────────────────────
//
// We combine two independent similarity measures and take the maximum.
// This mirrors the approach of Python's `fuzzywuzzy` / `rapidfuzz` libraries.
//
// ── Measure A: Levenshtein ratio ──────────────────────────────────────────
//   The Levenshtein (edit) distance d(a, b) is the minimum number of
//   single-character operations (insert / delete / substitute) needed to
//   transform string a into string b.
//
//   We convert it to a 0–1 similarity ratio:
//
//       ratio(a, b) = 1  −  d(a, b) / max(|a|, |b|)
//
//   Example:
//     a = "San Francisco 1904"  (|a| = 18)
//     b = "San Francisco 1905"  (|b| = 18)
//     Only the last digit differs → d = 1
//     ratio = 1 − 1/18 ≈ 0.944  (94.4 % similar ✓)
//
// ── Measure B: Jaccard word similarity ────────────────────────────────────
//   Treats each string as a *set* of words (tokens) and computes:
//
//       J(A, B) = |A ∩ B| / |A ∪ B|
//
//   Example:
//     A = {"san","francisco","telephone","directory","1904"}
//     B = {"san","francisco","telephone","directory","1905"}
//     |A ∩ B| = 4   |A ∪ B| = 6   →   J = 4/6 ≈ 0.667
//
//   Jaccard handles word-order differences and partial overlap better than
//   Levenshtein when titles share most words but differ in one number/year.
//
// ── Final score ───────────────────────────────────────────────────────────
//   fuzzyScore(a, b) = max(levenshteinRatio(a, b), jaccardWords(a, b))
//
//   Taking the max means: if *either* measure decides two strings are
//   similar, we trust it.  This reduces false negatives (missed matches).

/**
 * Normalise a title string for comparison:
 *   • lowercase
 *   • replace punctuation/special chars with spaces
 *   • collapse multiple spaces
 */
function normTitle(str) {
  return (str || '')
    .toLowerCase()
    .replace(/[^\w\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Classic dynamic-programming Levenshtein distance.
 * Time: O(m·n)  Space: O(min(m,n))  (two-row optimisation).
 *
 * @param {string} a
 * @param {string} b
 * @returns {number} edit distance
 */
function levenshtein(a, b) {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;

  // Keep the shorter string in the inner dimension to minimise allocations
  if (a.length > b.length) [a, b] = [b, a];

  let prev = Array.from({ length: a.length + 1 }, (_, i) => i);
  let curr = new Array(a.length + 1);

  for (let j = 1; j <= b.length; j++) {
    curr[0] = j;
    for (let i = 1; i <= a.length; i++) {
      curr[i] = a[i - 1] === b[j - 1]
        ? prev[i - 1]
        : 1 + Math.min(prev[i - 1], prev[i], curr[i - 1]);
    }
    [prev, curr] = [curr, prev];
  }
  return prev[a.length];
}

/**
 * Levenshtein-based similarity ratio in [0, 1].
 * 1.0 = identical strings, 0.0 = completely different.
 */
function levenshteinRatio(a, b) {
  const na = normTitle(a), nb = normTitle(b);
  if (!na && !nb) return 1;
  if (!na || !nb) return 0;
  const maxLen = Math.max(na.length, nb.length);
  return 1 - levenshtein(na, nb) / maxLen;
}

/**
 * Jaccard similarity on word tokens in [0, 1].
 * J(A, B) = |A ∩ B| / |A ∪ B|
 */
function jaccardWords(a, b) {
  const tokensA = new Set(normTitle(a).split(' ').filter(Boolean));
  const tokensB = new Set(normTitle(b).split(' ').filter(Boolean));
  if (tokensA.size === 0 && tokensB.size === 0) return 1;
  if (tokensA.size === 0 || tokensB.size === 0) return 0;

  let intersection = 0;
  for (const t of tokensA) { if (tokensB.has(t)) intersection++; }
  const union = tokensA.size + tokensB.size - intersection;
  return intersection / union;
}

/**
 * Combined fuzzy score — maximum of both measures.
 * @param {string} a
 * @param {string} b
 * @returns {number} similarity in [0, 1]
 */
function fuzzyScore(a, b) {
  return Math.max(levenshteinRatio(a, b), jaccardWords(a, b));
}

/**
 * Scan the current page for all harvestable items and return those whose
 * title is ≥ `threshold` similar to `referenceTitle`.
 *
 * Returns an array of { entry, score, title } objects sorted by score desc.
 *
 * @param {string} referenceTitle  — the title of the item the user right-clicked
 * @param {number} threshold       — 0–1, default 0.75
 * @returns {Array<{entry: Object, score: number, title: string}>}
 */
function findSimilarOnPage(referenceTitle, threshold = 0.75) {
  if (!referenceTitle || !referenceTitle.trim()) return [];

  // Ask each profile to enumerate item candidates from the current DOM.
  // We collect all candidates across all profiles (a page only belongs to
  // one archive, but multiple profiles might partially match it).
  const seen = new Set();   // deduplicate by URL
  const candidates = [];

  for (const profile of PROFILES) {
    if (typeof profile.enumerateItems !== 'function') continue;
    try {
      const items = profile.enumerateItems(document);
      for (const item of items) {
        if (!item.url || seen.has(item.url)) continue;
        seen.add(item.url);
        candidates.push({ profile, ...item });
      }
    } catch (err) {
      console.warn(`[HQB] enumerateItems threw in "${profile.name}":`, err);
    }
  }

  // Score each candidate and filter by threshold
  const results = [];
  for (const candidate of candidates) {
    const score = fuzzyScore(referenceTitle, candidate.title);
    if (score >= threshold) {
      // Build the queue entry using the profile's buildEntry
      let entry = null;
      try {
        entry = candidate.profile.buildEntry(candidate.url, candidate.el);
      } catch (_) { /* skip */ }
      if (entry) {
        results.push({ entry, score, title: candidate.title });
      }
    }
  }

  // Sort best match first
  results.sort((a, b) => b.score - a.score);
  return results;
}


// ─────────────────────────────────────────────────────────────────────────────
// 4.  Site profiles
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Generic item enumerator: given CSS selectors for item containers and the
 * link/title elements within them, extract { el, url, title } for every card
 * visible in the document.
 *
 * @param {Document} doc
 * @param {string[]} containerSelectors  — CSS selectors tried in order for the card wrapper
 * @param {string[]} linkSelectors       — CSS selectors for the <a> inside the card
 * @param {string[]} titleSelectors      — CSS selectors for the title element
 * @returns {Array<{el, url, title}>}
 */
function genericEnumerate(doc, containerSelectors, linkSelectors, titleSelectors) {
  let containers = [];
  for (const cs of containerSelectors) {
    const found = [...doc.querySelectorAll(cs)];
    if (found.length > 0) { containers = found; break; }
  }

  const items = [];
  for (const container of containers) {
    // Find the primary link
    let linkEl = null;
    for (const ls of linkSelectors) {
      linkEl = container.querySelector(ls);
      if (linkEl) break;
    }
    if (!linkEl && container.tagName === 'A') linkEl = container;

    const href = linkEl?.getAttribute('href');
    if (!href) continue;

    let url;
    try { url = new URL(href, window.location.href).href; } catch { continue; }

    // Find the title text
    let title = '';
    for (const ts of titleSelectors) {
      const titleEl = container.querySelector(ts);
      if (titleEl) {
        title = (titleEl.getAttribute('title') || titleEl.textContent || '').trim();
        if (title) break;
      }
    }
    if (!title) title = (linkEl.getAttribute('title') || linkEl.textContent || '').trim();
    if (!title) continue;

    items.push({ el: container, url, title });
  }
  return items;
}

const PROFILES = [

  // ── Archive.org ────────────────────────────────────────────────────────────
  {
    name: 'archive_org',
    test: (url) => /archive\.org\/(details|download)\//.test(url),

    buildEntry(url, el) {
      const m = url.match(/archive\.org\/(?:details|download)\/([^/?#\s]+)/);
      if (!m) return null;
      const id = m[1];
      const note = sanitise(nearestText(el,
        ['[data-id]', '.item-ia', '.tile-details', 'article', '.result'],
        ['h3[title]', 'h2[title]', 'h3', 'h2', '[title]', '.item-title'],
      ));
      return { source: 'archive_org', id, note };
    },

    enumerateItems(doc) {
      // Archive.org search-results page: each result is a <tile-ia> or
      // a div with class "item-ia" or ".result" containing an <a> to /details/
      return genericEnumerate(doc,
        ['tile-ia', '.item-ia', '.result', '[data-id]', '.iaux-item-list__item'],
        ['a[href*="/details/"]', 'a'],
        ['h3[title]', 'h2[title]', 'h3', 'h2', '[title]', '.item-title'],
      ).filter(item => /archive\.org\/(details|download)\//.test(item.url));
    },
  },

  // ── Polona.pl ──────────────────────────────────────────────────────────────
  {
    name: 'polona',
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

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['[class*="search-result"]', '[class*="card"]', 'article', 'li'],
        ['a[href*="/item/"]', 'a[href*="/preview/"]', 'a'],
        ['h2[title]', 'h3[title]', 'h2', 'h3', '.title'],
      ).filter(item => /polona\.pl\/(item|preview|pl)\//.test(item.url));
    },
  },

  // ── FBC / Pionier (dLibra consortium portal) ───────────────────────────────
  {
    name: 'fbc',
    test: (url) => /fbc\.pionier\.net\.pl/.test(url),

    buildEntry(url, el) {
      const cleanUrl = url.split('?')[0].split('#')[0];
      const note = sanitise(nearestText(el,
        ['.result', 'article', 'tr', 'li', '[class*="item"]'],
        ['h2', 'h3', 'a', 'td', '.title'],
      ));
      return { source: 'dlibra', url: cleanUrl, note };
    },

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['.result', 'article', 'tr', 'li[class*="result"]'],
        ['a[href*="fbc.pionier"]', 'a'],
        ['h2', 'h3', '.title', 'td'],
      ).filter(item => /fbc\.pionier\.net\.pl/.test(item.url));
    },
  },

  // ── Any dLibra-powered site ────────────────────────────────────────────────
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

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['.result', 'article', 'tr', 'li'],
        ['a[href*="/publication/"]', 'a[href*="/Content/"]', 'a'],
        ['h2', 'h3', '.title', 'td'],
      ).filter(item => /\/(publication|Content|dlibra)\//.test(item.url));
    },
  },

  // ── Bayerische Staatsbibliothek ────────────────────────────────────────────
  {
    name: 'bsb',
    test: (url) => /digitale-sammlungen\.de/.test(url),

    buildEntry(url, el) {
      const note = sanitise(nearestText(el,
        ['article', '.result', '[class*="teaser"]', '[class*="item"]'],
        ['h2', 'h3', '.title'],
      ));
      return { source: 'dlibra', url, note };
    },

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['article', '[class*="teaser"]', '[class*="result"]'],
        ['a[href*="/view/"]', 'a'],
        ['h2', 'h3', '.title'],
      ).filter(item => /digitale-sammlungen\.de/.test(item.url));
    },
  },

  // ── Gallica (BnF) ──────────────────────────────────────────────────────────
  {
    name: 'gallica',
    test: (url) => /gallica\.bnf\.fr/.test(url),

    buildEntry(url, el) {
      const m = url.match(/gallica\.bnf\.fr\/ark:\/12148\/([^/?#\s]+)/);
      const arkId = m ? m[1] : null;
      const note = sanitise(nearestText(el,
        ['.result-item', '.item', 'article', '[class*="result"]'],
        ['h2', 'h3', '.title', '[title]'],
      ));
      return { source: 'dlibra', url, note: arkId ? `ark:${arkId} — ${note}` : note };
    },

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['[class*="result"]', 'article', '.item', 'li'],
        ['a[href*="ark:/12148"]', 'a'],
        ['h2', 'h3', '.title', '[title]'],
      ).filter(item => /gallica\.bnf\.fr/.test(item.url));
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

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['article', '[class*="item"]', '[class*="result"]', '.card'],
        ['a'],
        ['h2', 'h3', '.title'],
      ).filter(item => /onb\.ac\.at/.test(item.url));
    },
  },

  // ── Deutsche Digitale Bibliothek ───────────────────────────────────────────
  {
    name: 'ddb',
    test: (url) => /deutsche-digitale-bibliothek\.de/.test(url),

    buildEntry(url, el) {
      const note = sanitise(nearestText(el,
        ['article', '[class*="teaser"]', '[class*="result"]', '.card'],
        ['h2', 'h3', '.title'],
      ));
      return { source: 'dlibra', url, note };
    },

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['article', '[class*="teaser"]', '[class*="result"]'],
        ['a[href*="/item/"]', 'a'],
        ['h2', 'h3', '.title'],
      ).filter(item => /deutsche-digitale-bibliothek\.de/.test(item.url));
    },
  },

  // ── Europeana ───────────────────────────────────────────────────────────────
  {
    name: 'europeana',
    test: (url) => /europeana\.eu/.test(url),

    buildEntry(url, el) {
      const m = url.match(/europeana\.eu\/[^/]+\/item\/([^?#\s]+)/);
      const euroId = m ? m[1] : null;
      const note = sanitise(nearestText(el,
        ['[class*="card"]', 'article', '[class*="item"]', '[class*="result"]'],
        ['h2', 'h3', '.title', '[title]'],
      ));
      return { source: 'dlibra', url, note: euroId ? `europeana:${euroId} — ${note}` : note };
    },

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['[class*="card"]', 'article', '[class*="item"]'],
        ['a[href*="/item/"]', 'a'],
        ['h2', 'h3', '.title', '[title]'],
      ).filter(item => /europeana\.eu/.test(item.url));
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
        return {
          source: 'szwa',
          url,
          note: note || '⚠ Add zespol + jednostka manually',
        };
      } catch {
        return { source: 'szwa', url, note: '' };
      }
    },

    enumerateItems(doc) {
      return genericEnumerate(doc,
        ['tr', 'article', '.result', 'li'],
        ['a[href*="szukajwarchiwach"]', 'a[href*="zespol"]', 'a'],
        ['td', 'h2', 'h3'],
      ).filter(item => /szukajwarchiwach\.gov\.pl/.test(item.url));
    },
  },

]; // end PROFILES


// ─────────────────────────────────────────────────────────────────────────────
// 5.  Entry-point: resolve a URL against all profiles
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
// 6.  Message listener (called by background.js)
// ─────────────────────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {

  // ── Extract item data ──────────────────────────────────────────────────────
  if (msg.action === 'extractItemData') {
    const entry = extractItemData(msg.targetUrl, _lastRightClicked);
    sendResponse(entry);
    return true;
  }

  // ── Find similar items on this page ───────────────────────────────────────
  // Called by background after a save, OR directly from the popup's
  // "find similar" button on an existing queue item.
  //
  // msg.referenceTitle  — title string to match against
  // msg.threshold       — optional float 0–1, default 0.75
  //
  // Returns Array<{ entry, score, title }>
  if (msg.action === 'findSimilar') {
    const threshold = typeof msg.threshold === 'number' ? msg.threshold : 0.75;
    const results   = findSimilarOnPage(msg.referenceTitle, threshold);
    sendResponse(results);
    return true;
  }

  // ── Show toast notification ────────────────────────────────────────────────
  if (msg.action === 'showToast') {
    showToast(msg.message, msg.type || 'saved');
    return;
  }
});


// ─────────────────────────────────────────────────────────────────────────────
// 7.  Toast UI
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
    similar:   { bg: '#1a2535', border: '#4fc3f7', color: '#81d4fa' },
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
