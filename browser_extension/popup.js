/**
 * popup.js — Harvest Queue Builder
 *
 * Manages the extension popup UI:
 *   - Renders the queue from chrome.storage.local
 *   - Download as harvest_queue.json (strips internal `_idx` helper field)
 *   - Copy JSON to clipboard
 *   - Remove individual items
 *   - Inline note editing (click a note to edit it)
 *   - Filter/search across source, id, url, note
 *   - Clear-all with confirmation
 *   - Live badge update
 */

'use strict';

// ─── State ────────────────────────────────────────────────────────────────────

/** Full queue from storage — mutated in place and persisted after every change */
let queue = [];

/** Current filter string */
let filterText = '';


// ─── DOM refs ─────────────────────────────────────────────────────────────────

const $list    = document.getElementById('queue-list');
const $empty   = document.getElementById('empty-state');
const $badge   = document.getElementById('badge');
const $stats   = document.getElementById('footer-stats');
const $filter  = document.getElementById('filter-input');
const $overlay = document.getElementById('confirm-overlay');


// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Human-readable label for each source key */
const SOURCE_LABEL = {
  archive_org: 'archive.org',
  polona:      'polona.pl',
  dlibra:      'dLibra',
  szwa:        'SzWA',
  unknown:     'unknown',
};

/**
 * Return the "primary identifier" string for an entry:
 * used as the main line in the card.
 *
 *   archive_org / polona  →  entry.id
 *   szwa                  →  "zespol={x}  jednostka={y}"
 *   dlibra / unknown      →  entry.url (trimmed)
 */
function itemIdentifier(entry) {
  if (entry.id)       return entry.id;
  if (entry.zespol)   return `zespol=${entry.zespol}  jednostka=${entry.jednostka}`;
  if (entry.url)      return entry.url.replace(/^https?:\/\//, '');
  return '—';
}

/**
 * Serialise the queue for export — removes internal helper fields.
 * Omit empty `note` fields to keep the JSON tidy.
 */
function toExportJson(q) {
  return q.map(entry => {
    const out = { ...entry };
    if (!out.note) delete out.note;
    return out;
  });
}

/** Match an entry against the current filter string. */
function matchesFilter(entry, text) {
  if (!text) return true;
  const needle = text.toLowerCase();
  const haystack = [
    entry.source, entry.id, entry.url, entry.note,
    entry.zespol, entry.jednostka,
  ].filter(Boolean).join(' ').toLowerCase();
  return haystack.includes(needle);
}


// ─── Render ───────────────────────────────────────────────────────────────────

function render() {
  $list.innerHTML = '';

  const visible = queue.filter(e => matchesFilter(e, filterText));

  if (queue.length === 0) {
    $empty.style.display = 'flex';
    $list.style.display  = 'none';
  } else {
    $empty.style.display = 'none';
    $list.style.display  = 'flex';
  }

  visible.forEach((entry, visIdx) => {
    // Resolve the true index in the full queue for splice operations
    const realIdx = queue.indexOf(entry);

    const card    = document.createElement('div');
    const srcKey  = entry.source || 'unknown';
    card.className = `item-card ${srcKey === 'unknown' ? 'unknown' : ''}`;
    card.dataset.idx = realIdx;

    // ── Source pill ───────────────────────────────────────────────────────────
    const pill = document.createElement('span');
    pill.className = `item-source source-${srcKey}`;
    pill.textContent = SOURCE_LABEL[srcKey] || srcKey;

    // ── Primary identifier ────────────────────────────────────────────────────
    const idEl   = document.createElement('div');
    idEl.className = 'item-id';
    idEl.textContent = itemIdentifier(entry);
    idEl.title = itemIdentifier(entry);

    // ── Note (editable) ───────────────────────────────────────────────────────
    const noteEl = document.createElement('div');
    noteEl.className = 'item-note';
    noteEl.textContent = entry.note || '(no note — click to add)';
    noteEl.title = 'Click to edit note';
    noteEl.style.cursor = 'text';

    const noteInput = document.createElement('input');
    noteInput.className   = 'item-note-input';
    noteInput.type        = 'text';
    noteInput.value       = entry.note || '';
    noteInput.placeholder = 'Add a note…';
    noteInput.maxLength   = 120;

    // Toggle edit mode on note click
    noteEl.addEventListener('click', () => {
      card.classList.add('editing');
      noteInput.focus();
      noteInput.select();
    });

    // Save on blur or Enter
    const saveNote = () => {
      card.classList.remove('editing');
      const newNote = noteInput.value.trim();
      queue[realIdx].note = newNote;
      noteEl.textContent = newNote || '(no note — click to add)';
      persist();
    };
    noteInput.addEventListener('blur', saveNote);
    noteInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); saveNote(); }
      if (e.key === 'Escape') {
        noteInput.value = entry.note || '';
        card.classList.remove('editing');
      }
    });

    // ── URL (secondary, shown for dlibra/unknown) ──────────────────────────
    const urlEl = document.createElement('div');
    urlEl.className = 'item-url';
    if (entry.url && entry.source !== 'archive_org' && entry.source !== 'polona') {
      // Show the URL only for url-based sources
    } else {
      urlEl.style.display = 'none';
    }

    // ── Body ──────────────────────────────────────────────────────────────────
    const body = document.createElement('div');
    body.className = 'item-body';
    body.append(pill, idEl, noteEl, noteInput);

    // ── Remove button ─────────────────────────────────────────────────────────
    const removeBtn = document.createElement('button');
    removeBtn.className = 'item-remove';
    removeBtn.title = 'Remove from queue';
    removeBtn.innerHTML = '×';
    removeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      removeItem(realIdx);
    });

    // ── Find-similar button ───────────────────────────────────────────────────
    const simBtn = document.createElement('button');
    simBtn.className = 'item-remove similar';
    simBtn.title = 'Find similar items on active tab';
    simBtn.innerHTML = '≈';
    simBtn.style.cssText = 'font-size:15px;margin-top:0;';
    simBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openSimilarModal(entry);
    });

    card.append(body, simBtn, removeBtn);
    $list.appendChild(card);
  });

  // ── Badge and stats ──────────────────────────────────────────────────────
  $badge.textContent = queue.length;

  const sources = new Set(queue.map(e => e.source));
  $stats.textContent = `${queue.length} item${queue.length !== 1 ? 's' : ''} · ${sources.size} source${sources.size !== 1 ? 's' : ''}`;

  if (filterText && visible.length === 0 && queue.length > 0) {
    const msg = document.createElement('div');
    msg.className = 'empty-state';
    msg.style.cssText = 'display:flex;font-size:11px;color:var(--muted);text-align:center;padding:24px';
    msg.textContent = `No items match "${filterText}"`;
    $list.appendChild(msg);
  }
}


// ─── Storage helpers ──────────────────────────────────────────────────────────

async function loadQueue() {
  const { harvestQueue = [] } = await chrome.storage.local.get('harvestQueue');
  queue = harvestQueue;
  render();
}

async function persist() {
  await chrome.storage.local.set({ harvestQueue: queue });
  // Sync badge in background worker
  chrome.runtime.sendMessage({ action: '_syncBadge' }).catch(() => {});
}

function removeItem(idx) {
  queue.splice(idx, 1);
  persist();
  render();
}


// ─── Download ─────────────────────────────────────────────────────────────────

function downloadJson() {
  const json = JSON.stringify(toExportJson(queue), null, 2);
  const blob = new Blob([json], { type: 'application/json' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = 'harvest_queue.json';
  a.click();
  URL.revokeObjectURL(url);
}

async function copyJson() {
  const json = JSON.stringify(toExportJson(queue), null, 2);
  await navigator.clipboard.writeText(json);

  const btn = document.getElementById('btn-copy');
  const original = btn.innerHTML;
  btn.innerHTML = '✓ Copied!';
  btn.style.color = 'var(--green)';
  setTimeout(() => {
    btn.innerHTML = original;
    btn.style.color = '';
  }, 1500);
}


// ─── Event wiring ─────────────────────────────────────────────────────────────

document.getElementById('btn-download').addEventListener('click', downloadJson);
document.getElementById('btn-copy').addEventListener('click', copyJson);

// Clear-all with confirmation overlay
document.getElementById('btn-clear').addEventListener('click', () => {
  if (queue.length === 0) return;
  $overlay.classList.add('active');
});
document.getElementById('confirm-yes').addEventListener('click', async () => {
  queue = [];
  await persist();
  render();
  $overlay.classList.remove('active');
});
document.getElementById('confirm-no').addEventListener('click', () => {
  $overlay.classList.remove('active');
});

// Filter
$filter.addEventListener('input', () => {
  filterText = $filter.value;
  render();
});

// Storage change listener — keeps popup live if background saves while open
chrome.storage.onChanged.addListener((changes) => {
  if (changes.harvestQueue) {
    queue = changes.harvestQueue.newValue || [];
    render();
  }
});

// ─── Similar items modal ──────────────────────────────────────────────────────

const $simOverlay   = document.getElementById('similar-overlay');
const $simRefTitle  = document.getElementById('similar-ref-title');
const $simList      = document.getElementById('similar-list');
const $simThreshold = document.getElementById('similar-threshold');
const $simLabel     = document.getElementById('similar-threshold-label');

/** The entry whose "≈" button was clicked — used as the fuzzy reference */
let _simSourceEntry = null;

/** Raw results from the last scan: Array<{entry, score, title}> */
let _simResults = [];

/**
 * Open the modal and kick off a scan for items similar to `sourceEntry`.
 * We message the content script on the currently-active tab.
 */
async function openSimilarModal(sourceEntry) {
  _simSourceEntry = sourceEntry;
  $simOverlay.classList.add('active');

  // Show the reference title the user is matching against
  const refTitle = sourceEntry.note || sourceEntry.id
    || (sourceEntry.url || '').split('/').pop() || '(unknown title)';
  $simRefTitle.textContent = `Matching: "${refTitle}"`;

  await runScan(refTitle);
}

async function runScan(refTitle) {
  const threshold = parseInt($simThreshold.value, 10) / 100;

  $simList.innerHTML = '<div class="similar-scanning">⏳ Scanning page…</div>';
  document.getElementById('similar-add-selected').disabled = true;

  // Get the active tab in the current window
  let tabId = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    tabId = tab?.id;
  } catch (_) { /* permissions edge case */ }

  if (!tabId) {
    $simList.innerHTML = '<div class="similar-none">⚠ Could not reach the active tab.<br>Navigate to an archive page first.</div>';
    return;
  }

  try {
    _simResults = await chrome.tabs.sendMessage(tabId, {
      action: 'findSimilar',
      referenceTitle: refTitle,
      threshold,
    });
  } catch (err) {
    $simList.innerHTML = '<div class="similar-none">⚠ Content script not reachable.<br>Reload the archive tab and try again.</div>';
    return;
  }

  // Filter out items already in the queue
  _simResults = (_simResults || []).filter(
    ({ entry }) => !queue.some(e => isSameItemPopup(e, entry))
  );

  renderSimResults();
  document.getElementById('similar-add-selected').disabled = false;
}

/**
 * Render the similarity result cards inside the modal.
 * Each card has a checkbox, the title, a score badge, and the source/id.
 */
function renderSimResults() {
  $simList.innerHTML = '';

  if (_simResults.length === 0) {
    $simList.innerHTML = '<div class="similar-none">No new similar items found on this page<br>at the current threshold.</div>';
    return;
  }

  for (let i = 0; i < _simResults.length; i++) {
    const { entry, score, title } = _simResults[i];
    const pct = Math.round(score * 100);

    const item = document.createElement('label');
    item.className = 'sim-item';
    item.htmlFor = `sim-cb-${i}`;

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id   = `sim-cb-${i}`;
    cb.checked = true;  // default: select all found matches
    cb.addEventListener('change', () => {
      item.classList.toggle('checked', cb.checked);
    });
    item.classList.add('checked');

    const body = document.createElement('div');
    body.className = 'sim-item-body';

    const titleEl = document.createElement('div');
    titleEl.className = 'sim-item-title';
    titleEl.textContent = title || entry.note || entry.id || entry.url || '—';
    titleEl.title = titleEl.textContent;

    const metaEl = document.createElement('div');
    metaEl.className = 'sim-item-meta';
    // Show the source identifier inline
    const srcLabel = { archive_org:'archive.org', polona:'polona.pl', dlibra:'dLibra', szwa:'SzWA', unknown:'?' };
    const idStr = entry.id || entry.url?.replace(/^https?:\/\//, '') || '';
    metaEl.textContent = `${srcLabel[entry.source] || entry.source} — ${idStr.slice(0, 60)}`;

    body.append(titleEl, metaEl);

    const scoreBadge = document.createElement('div');
    const cls = pct >= 90 ? 'high' : pct >= 75 ? 'medium' : 'low';
    scoreBadge.className = `sim-score ${cls}`;
    scoreBadge.textContent = `${pct}%`;
    scoreBadge.title = `Fuzzy similarity: ${pct}%`;

    item.append(cb, body, scoreBadge);
    $simList.appendChild(item);
  }
}

/** Popup-side duplicate check (mirrors background.js isSameItem) */
function isSameItemPopup(a, b) {
  if (a.source !== b.source) return false;
  if (a.id  && b.id)  return a.id  === b.id;
  if (a.url && b.url) return a.url === b.url;
  if (a.zespol && b.zespol) return a.zespol === b.zespol && a.jednostka === b.jednostka;
  return false;
}

// ─── Similar modal event wiring ───────────────────────────────────────────────

document.getElementById('similar-close').addEventListener('click', () => {
  $simOverlay.classList.remove('active');
});

/** Sync preset button highlight and label to a given value (string or number) */
function syncThresholdUI(value) {
  const v = parseInt(value, 10);
  $simThreshold.value   = v;
  $simLabel.textContent = `${v}%`;
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.classList.toggle('active', parseInt(btn.dataset.value, 10) === v);
  });
}

// Preset buttons → update controls (no auto-rescan; user clicks ↺ to apply)
document.querySelectorAll('.preset-btn').forEach(btn => {
  btn.addEventListener('click', () => syncThresholdUI(btn.dataset.value));
});

// Manual slider drag → sync label + clear preset highlight if between presets
$simThreshold.addEventListener('input', () => {
  syncThresholdUI($simThreshold.value);
});

document.getElementById('similar-rescan').addEventListener('click', async () => {
  if (!_simSourceEntry) return;
  const refTitle = _simSourceEntry.note || _simSourceEntry.id
    || (_simSourceEntry.url || '').split('/').pop() || '';
  await runScan(refTitle);
});

document.getElementById('similar-select-all').addEventListener('click', () => {
  $simList.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.checked = true;
    cb.closest('.sim-item')?.classList.add('checked');
  });
});

document.getElementById('similar-select-none').addEventListener('click', () => {
  $simList.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.checked = false;
    cb.closest('.sim-item')?.classList.remove('checked');
  });
});

document.getElementById('similar-add-selected').addEventListener('click', async () => {
  const checkboxes = [...$simList.querySelectorAll('input[type=checkbox]')];
  const toAdd = checkboxes
    .map((cb, i) => cb.checked ? _simResults[i] : null)
    .filter(Boolean);

  if (toAdd.length === 0) {
    $simOverlay.classList.remove('active');
    return;
  }

  let added = 0;
  for (const { entry } of toAdd) {
    if (!queue.some(e => isSameItemPopup(e, entry))) {
      queue.push(entry);
      added++;
    }
  }

  if (added > 0) {
    await persist();
    render();
  }

  $simOverlay.classList.remove('active');

  // Brief confirmation flash on the badge
  $badge.style.background = '#4caf6e';
  $badge.textContent = `+${added}`;
  setTimeout(() => {
    $badge.style.background = '';
    $badge.textContent = queue.length;
  }, 1200);
});

// ── Boot ────────────────────────────────────────────────────────────────────
loadQueue();
