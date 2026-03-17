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

    card.append(body, removeBtn);
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

// ── Boot ────────────────────────────────────────────────────────────────────
loadQueue();
