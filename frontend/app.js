/* ── Environment ── */
const API_BASE = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
  ? ''
  : 'https://award-availability-test-production.up.railway.app';

/* ── State ── */
let allResults = [];
let currentSearchId = null;
let eventSource = null;
let currentPayload = null;

/* ── Selection state ── */
let selectedOutboundKey = null;
let selectedInboundKey  = null;
let selectedOutboundRow = null;
let selectedInboundRow  = null;

/* ── Airport multi-select state ── */
let allAirports = [];
const selectedCodes = new Set();

/* ── OTP modal ── */
const otpModal     = document.getElementById('otpModal');
const otpSubmitBtn = document.getElementById('otpSubmitBtn');

function otpBoxes() { return [...document.querySelectorAll('.otp-box')]; }

document.getElementById('otpInputs').addEventListener('input', e => {
  if (!e.target.classList.contains('otp-box')) return;
  e.target.value = e.target.value.replace(/\D/g, '').slice(0, 1);
  const boxes = otpBoxes();
  const idx = boxes.indexOf(e.target);
  if (e.target.value && idx < 5) boxes[idx + 1].focus();
  otpSubmitBtn.disabled = boxes.some(b => !b.value);
});

document.getElementById('otpInputs').addEventListener('keydown', e => {
  if (!e.target.classList.contains('otp-box')) return;
  const boxes = otpBoxes();
  const idx = boxes.indexOf(e.target);
  if (e.key === 'Backspace' && !e.target.value && idx > 0) boxes[idx - 1].focus();
  if (e.key === 'Enter' && !otpSubmitBtn.disabled) otpSubmitBtn.click();
});

document.getElementById('otpInputs').addEventListener('paste', e => {
  e.preventDefault();
  const text = (e.clipboardData || window.clipboardData).getData('text').replace(/\D/g, '').slice(0, 6);
  const boxes = otpBoxes();
  text.split('').forEach((ch, i) => { if (boxes[i]) boxes[i].value = ch; });
  otpSubmitBtn.disabled = boxes.some(b => !b.value);
  const next = boxes[text.length] || boxes[5];
  next.focus();
});

otpSubmitBtn.addEventListener('click', async () => {
  const code = otpBoxes().map(b => b.value).join('');
  if (code.length !== 6 || !currentSearchId) return;
  otpSubmitBtn.disabled = true;
  otpSubmitBtn.textContent = 'Submitting…';
  try {
    await fetch(`${API_BASE}/api/search/${currentSearchId}/otp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
  } catch (_) {}
  otpModal.classList.add('hidden');
  otpBoxes().forEach(b => { b.value = ''; });
  otpSubmitBtn.disabled = true;
  otpSubmitBtn.textContent = 'Submit Code';
});

/* ── DOM refs ── */
const form           = document.getElementById('searchForm');
const searchBtn      = document.getElementById('searchBtn');
const cancelBtn      = document.getElementById('cancelBtn');
const progressSec    = document.getElementById('progressSection');
const progressBar    = document.getElementById('progressBar');
const progressText   = document.getElementById('progressText');
const progressEta    = document.getElementById('progressEta');
const loginPrompt    = document.getElementById('loginPrompt');
const loginMsg       = document.getElementById('loginMsg');
const continueBtn    = document.getElementById('continueBtn');
const resultsSec     = document.getElementById('resultsSection');
const resultsSummary = document.getElementById('resultsSummary');
const searchEstimate = document.getElementById('searchEstimate');
const availableOnly  = document.getElementById('availableOnly');
const exportBtn      = document.getElementById('exportBtn');
const page1          = document.getElementById('page1');
const page2          = document.getElementById('page2');
const backBtn        = document.getElementById('backBtn');
const flightsLayout  = document.getElementById('flightsLayout');
const inboundCol     = document.getElementById('inboundCol');

/* ── Trip type toggle ── */
document.querySelectorAll('input[name="tripType"]').forEach(radio => {
  radio.addEventListener('change', () => {
    const isReturn = radio.value === 'return';
    document.querySelector('.return-only').classList.toggle('hidden-field', !isReturn);
    updateEstimate();
  });
});

/* ── Live search count estimate ── */
function calcSearchCount() {
  const destinations = getDestinations();
  const tripType = document.querySelector('input[name="tripType"]:checked')?.value;
  const cabins = getSelectedCabins();
  const depStart  = document.getElementById('depStart').value;
  const isFlexDep = document.getElementById('flexDep')?.checked;
  const depEnd    = isFlexDep ? document.getElementById('depEnd').value : depStart;
  const isFlexNights = document.getElementById('flexNights')?.checked;
  const minN = isFlexNights
    ? parseInt(document.getElementById('minNights').value) || 1
    : parseInt(document.getElementById('exactNights').value) || 7;
  const maxN = isFlexNights
    ? parseInt(document.getElementById('maxNights').value) || 7
    : parseInt(document.getElementById('exactNights').value) || 7;

  if (!destinations.length || !depStart || !depEnd) return null;

  const startDate = new Date(depStart);
  const endDate   = new Date(depEnd);
  if (endDate < startDate) return null;

  const depDays = Math.floor((endDate - startDate) / 86400000) + 1;
  const nightsRange = tripType === 'return' ? Math.max(0, maxN - minN + 1) : 1;
  const total = depDays * destinations.length * cabins.length * nightsRange;
  return { total, depDays, nightsRange };
}

function updateEstimate() {
  const info = calcSearchCount();
  if (!info) { searchEstimate.classList.add('hidden'); return; }

  const mins = Math.round(info.total * 15 / 60);
  const isWarn = mins > 20;

  const nDest = getDestinations().length;
  const isFlexNightsDisp = document.getElementById('flexNights')?.checked;
  const tripTypeVal = document.querySelector('input[name="tripType"]:checked')?.value;
  searchEstimate.textContent =
    `${info.total} search${info.total !== 1 ? 'es' : ''} ` +
    `(~${mins < 1 ? '<1' : mins} min) — ` +
    `${info.depDays} departure day${info.depDays !== 1 ? 's' : ''} × ` +
    `${nDest} destination${nDest !== 1 ? 's' : ''}` +
    (tripTypeVal === 'return' && isFlexNightsDisp && info.nightsRange > 1
      ? ` × ${info.nightsRange} night options` : '');

  searchEstimate.className = 'search-estimate ' + (isWarn ? 'warn' : 'ok');
  searchEstimate.classList.remove('hidden');
}

['minNights', 'maxNights', 'exactNights', 'destinations'].forEach(id => {
  document.getElementById(id)?.addEventListener('input', updateEstimate);
});
document.querySelectorAll('input[name="cabin"]').forEach(cb => {
  cb.addEventListener('change', updateEstimate);
});
document.getElementById('flexNights').addEventListener('change', e => {
  document.getElementById('exactNightsWrap').classList.toggle('hidden', e.target.checked);
  document.getElementById('flexNightsRow').classList.toggle('hidden', !e.target.checked);
  updateEstimate();
});
document.getElementById('flexDep').addEventListener('change', e => {
  document.getElementById('depEndCol').classList.toggle('hidden', !e.target.checked);
  updateEstimate();
});

/* ── Airport multi-select widget ── */
async function loadAirports() {
  const hint = document.getElementById('destHint');
  try {
    const res = await fetch(`${API_BASE}/api/destinations`);
    if (!res.ok) throw new Error(res.statusText);
    allAirports = await res.json();
    hint.textContent = `— ${allAirports.length} destinations available`;
    initAirportWidget();
    initOriginWidget();
  } catch (_) {
    hint.textContent = '— enter comma-separated IATA codes';
    document.getElementById('airportWrap').classList.add('hidden');
    document.getElementById('destinations').classList.remove('hidden');
  }
}

function initAirportWidget() {
  const wrap     = document.getElementById('airportWrap');
  const tagsRow  = document.getElementById('airportTagsRow');
  const search   = document.getElementById('airportSearch');
  const dropdown = document.getElementById('airportDropdown');

  search.addEventListener('input', () => { renderDropdown(search.value.trim()); updateEstimate(); });

  search.addEventListener('keydown', e => {
    if (e.key === 'Backspace' && !search.value) {
      const codes = Array.from(selectedCodes);
      if (codes.length) { selectedCodes.delete(codes.at(-1)); renderTags(); updateEstimate(); }
    }
    if (e.key === 'Escape') dropdown.classList.add('hidden');
    if (e.key === 'Enter') { e.preventDefault(); dropdown.querySelector('.airport-opt.active')?.click(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const opts = [...dropdown.querySelectorAll('.airport-opt')];
      const idx  = opts.findIndex(o => o.classList.contains('active'));
      const next = e.key === 'ArrowDown' ? opts[idx + 1] ?? opts[0] : opts[idx - 1] ?? opts.at(-1);
      opts.forEach(o => o.classList.remove('active'));
      next?.classList.add('active');
      next?.scrollIntoView({ block: 'nearest' });
    }
  });

  search.addEventListener('focus', () => renderDropdown(search.value.trim()));

  document.addEventListener('click', e => {
    if (!wrap.contains(e.target)) dropdown.classList.add('hidden');
  });

  tagsRow.addEventListener('click', e => {
    if (e.target === tagsRow || e.target === wrap) search.focus();
  });

  document.getElementById('origin').addEventListener('change', () => {
    renderDropdown(search.value.trim());
  });
}

function renderTags() {
  const row    = document.getElementById('airportTagsRow');
  const search = document.getElementById('airportSearch');
  row.querySelectorAll('.airport-tag, .clear-all-tags').forEach(t => t.remove());
  for (const code of selectedCodes) {
    const tag = document.createElement('span');
    tag.className = 'airport-tag';
    tag.innerHTML = `${code} <span class="rm" data-code="${code}">×</span>`;
    tag.querySelector('.rm').addEventListener('click', e => {
      e.stopPropagation();
      selectedCodes.delete(code);
      renderTags();
      updateEstimate();
    });
    row.insertBefore(tag, search);
  }
  if (selectedCodes.size >= 1) {
    const btn = document.createElement('span');
    btn.className = 'clear-all-tags';
    btn.textContent = 'Remove all';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      selectedCodes.clear();
      renderTags();
      renderDropdown('');
      updateEstimate();
    });
    row.insertBefore(btn, search);
  }
}

function renderDropdown(query) {
  const dropdown  = document.getElementById('airportDropdown');
  const q         = query.toLowerCase();
  const originCode = document.getElementById('origin').value.trim().toUpperCase();
  let html = '';

  const available = allAirports.filter(a => !selectedCodes.has(a.code) && a.code !== originCode);

  const allByCountry = {};
  for (const a of available) {
    const c = a.country || 'Other';
    (allByCountry[c] = allByCountry[c] || []).push(a);
  }

  const sortCountries = arr => arr.sort((a, b) =>
    a === 'Other' ? 1 : b === 'Other' ? -1 : a.localeCompare(b)
  );

  const airportRow = a =>
    `<div class="airport-opt" data-code="${a.code}">
       <span class="ap-code">${a.code}</span>
       <span class="ap-name">${a.name}</span>
     </div>`;

  if (q) {
    const filtered = available.filter(a =>
      a.code.toLowerCase().includes(q) || a.name.toLowerCase().includes(q)
    );
    if (!filtered.length) { dropdown.classList.add('hidden'); return; }

    const matchedByCountry = {};
    for (const a of filtered) {
      const c = a.country || 'Other';
      (matchedByCountry[c] = matchedByCountry[c] || []).push(a);
    }
    for (const country of sortCountries(Object.keys(matchedByCountry))) {
      const matched   = matchedByCountry[country];
      const allInCtry = allByCountry[country] || matched;
      const allCodes  = allInCtry.map(a => a.code).join(',');
      html += `<div class="ap-group-hdr">${country}</div>`;
      if (allInCtry.length >= 2) {
        html += `<div class="airport-opt ap-all" data-all-country="${country}" data-codes="${allCodes}">+ All ${country}</div>`;
      }
      html += matched.map(airportRow).join('');
    }
  } else {
    const countries = sortCountries(Object.keys(allByCountry));
    if (!countries.length) { dropdown.classList.add('hidden'); return; }
    for (const country of countries) {
      const group = allByCountry[country];
      const codes = group.map(a => a.code).join(',');
      html += `<div class="ap-group-hdr">${country}</div>`;
      if (group.length >= 2) {
        html += `<div class="airport-opt ap-all" data-all-country="${country}" data-codes="${codes}">+ All ${country}</div>`;
      }
      html += group.map(airportRow).join('');
    }
  }

  if (!html) { dropdown.classList.add('hidden'); return; }
  dropdown.innerHTML = html;
  dropdown.querySelectorAll('.airport-opt').forEach(opt => {
    opt.addEventListener('click', () => {
      if (opt.dataset.allCountry) {
        opt.dataset.codes.split(',').forEach(c => selectedCodes.add(c));
      } else {
        selectedCodes.add(opt.dataset.code);
      }
      document.getElementById('airportSearch').value = '';
      renderTags();
      renderDropdown('');
      updateEstimate();
      document.getElementById('airportSearch').focus();
    });
    opt.addEventListener('mouseenter', () => {
      dropdown.querySelectorAll('.airport-opt').forEach(o => o.classList.remove('active'));
      opt.classList.add('active');
    });
  });
  dropdown.classList.remove('hidden');
}

/* ── Form helpers ── */
function getDestinations() {
  if (allAirports.length > 0) return Array.from(selectedCodes);
  return document.getElementById('destinations').value
    .split(',').map(d => d.trim().toUpperCase()).filter(d => d.length === 3);
}

function getSelectedCabins() {
  return Array.from(document.querySelectorAll('input[name="cabin"]:checked'))
    .map(cb => cb.value);
}

/* ── Submit ── */
form.addEventListener('submit', async e => {
  e.preventDefault();

  const destinations = getDestinations();
  if (!destinations.length) return alert('Enter at least one valid 3-letter airport code.');

  const cabins = getSelectedCabins();
  if (!cabins.length) return alert('Select at least one cabin class.');

  const tripType  = document.querySelector('input[name="tripType"]:checked').value;
  const depStart  = document.getElementById('depStart').value;
  const isFlexDep = document.getElementById('flexDep').checked;
  const depEnd    = isFlexDep ? document.getElementById('depEnd').value : depStart;
  if (!depStart) return alert('Select a departure date.');
  if (isFlexDep && !depEnd) return alert('Select an end date for the flexible departure range.');

  const isFlexNights = document.getElementById('flexNights').checked;
  const nightsMin = isFlexNights
    ? parseInt(document.getElementById('minNights').value)
    : parseInt(document.getElementById('exactNights').value);
  const nightsMax = isFlexNights
    ? parseInt(document.getElementById('maxNights').value)
    : parseInt(document.getElementById('exactNights').value);

  const payload = {
    origin:               document.getElementById('origin').value.trim().toUpperCase() || 'HKG',
    destinations,
    trip_type:            tripType,
    cabin_classes:        cabins,
    departure_date_start: depStart,
    departure_date_end:   depEnd,
    min_nights: tripType === 'return' ? nightsMin : null,
    max_nights: tripType === 'return' ? nightsMax : null,
  };

  startSearch(payload);
});

async function startSearch(payload) {
  allResults = [];
  currentPayload = payload;
  selectedOutboundKey = null;
  selectedInboundKey  = null;
  selectedOutboundRow = null;
  selectedInboundRow  = null;

  // Page transition
  page1.classList.add('hidden');
  page2.classList.remove('hidden');

  // Trip summary
  buildTripSummary(payload);

  // Show/hide return column
  const isReturn = payload.trip_type === 'return';
  inboundCol.classList.toggle('hidden', !isReturn);
  flightsLayout.classList.toggle('one-way', !isReturn);

  searchBtn.disabled = true;
  cancelBtn.classList.remove('hidden');
  progressSec.classList.remove('hidden');
  resultsSec.classList.remove('hidden');
  loginPrompt.classList.add('hidden');
  setProgress(0, 'Starting search…');

  let res;
  try {
    res = await fetch(`${API_BASE}/api/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    alert('Could not reach the server. Is the backend running?\n\nStart it with: ./start.sh');
    resetUI();
    return;
  }

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert('Search error: ' + (err.detail || res.statusText));
    resetUI();
    return;
  }

  const { search_id, total_searches } = await res.json();
  currentSearchId = search_id;

  eventSource = new EventSource(`${API_BASE}/api/results/${search_id}/stream`);

  eventSource.addEventListener('progress', e => {
    const d = JSON.parse(e.data);
    setProgress(d.current / d.total, `${d.current} / ${d.total} — ${d.message}`);
    const remaining = d.total - d.current;
    if (remaining > 0 && d.current > 0) {
      const secs = remaining * 15;
      progressEta.textContent = secs >= 60
        ? `~${Math.round(secs / 60)} min remaining`
        : `~${secs}s remaining`;
    } else {
      progressEta.textContent = '';
    }
  });

  eventSource.addEventListener('login', e => {
    const d = JSON.parse(e.data);
    loginPrompt.classList.remove('hidden');
    loginMsg.textContent = d.message;
  });

  eventSource.addEventListener('otp', () => {
    otpModal.classList.remove('hidden');
    setTimeout(() => otpBoxes()[0]?.focus(), 50);
  });

  eventSource.addEventListener('result', e => {
    const d = JSON.parse(e.data);
    if (d.data) {
      allResults.push(d.data);
      renderFlights();
    }
  });

  eventSource.addEventListener('complete', () => {
    setProgress(1, `Search complete — ${allResults.length} combinations checked.`);
    progressEta.textContent = '';
    loginPrompt.classList.add('hidden');
    otpModal.classList.add('hidden');
    resetUI(false);
  });

  eventSource.addEventListener('error', e => {
    try {
      const d = JSON.parse(e.data);
      alert('Search error: ' + d.message);
    } catch (_) {}
    otpModal.classList.add('hidden');
    resetUI();
  });

  eventSource.onerror = () => {
    eventSource.close();
  };
}

/* ── Continue after login ── */
continueBtn.addEventListener('click', async () => {
  if (!currentSearchId) return;
  continueBtn.disabled = true;
  continueBtn.textContent = 'Starting search…';
  await fetch(`${API_BASE}/api/search/${currentSearchId}/continue`, { method: 'POST' }).catch(() => {});
});

/* ── Cancel ── */
cancelBtn.addEventListener('click', async () => {
  if (!currentSearchId) return;
  eventSource?.close();
  await fetch(`${API_BASE}/api/search/${currentSearchId}`, { method: 'DELETE' }).catch(() => {});
  progressText.textContent = 'Search cancelled.';
  progressEta.textContent = '';
  resetUI();
});

/* ── Back to search ── */
backBtn.addEventListener('click', async () => {
  if (currentSearchId && eventSource) {
    eventSource.close();
    await fetch(`${API_BASE}/api/search/${currentSearchId}`, { method: 'DELETE' }).catch(() => {});
    eventSource = null;
    currentSearchId = null;
  }
  page2.classList.add('hidden');
  page1.classList.remove('hidden');
  searchBtn.disabled = false;
  cancelBtn.classList.add('hidden');
  allResults = [];
  currentPayload = null;
  selectedOutboundKey = null;
  selectedInboundKey  = null;
  selectedOutboundRow = null;
  selectedInboundRow  = null;
});

/* ── Progress helpers ── */
function setProgress(fraction, msg) {
  progressBar.style.width = `${Math.round(fraction * 100)}%`;
  progressText.textContent = msg;
}

function resetUI(clearProgress = true) {
  searchBtn.disabled = false;
  cancelBtn.classList.add('hidden');
  currentSearchId = null;
  eventSource = null;
}

/* ── Trip summary ── */
function buildTripSummary(payload) {
  const el = document.getElementById('tripSummary');
  const cabinLabels = {
    economy: 'Economy', premium_economy: 'Prem. Eco',
    business: 'Business', first: 'First',
  };
  const cabins = payload.cabin_classes.map(c => cabinLabels[c] || c).join(', ');
  const dests  = payload.destinations.join(', ');
  const isFlexDep = payload.departure_date_start !== payload.departure_date_end;
  const dateStr = isFlexDep
    ? `${formatDateShort(payload.departure_date_start)} – ${formatDateShort(payload.departure_date_end)}`
    : formatDateShort(payload.departure_date_start);

  const isReturn = payload.trip_type === 'return';
  const nightsStr = isReturn
    ? payload.min_nights === payload.max_nights
      ? ` · ${payload.min_nights}n`
      : ` · ${payload.min_nights}–${payload.max_nights}n`
    : '';

  el.innerHTML = `
    <span class="ts-route">${payload.origin} <span class="ts-arrow">→</span> ${dests}</span>
    <span class="ts-sep">·</span>
    <span class="ts-date">${dateStr}${nightsStr}</span>
    <span class="ts-sep">·</span>
    <span class="ts-cabin">${cabins}</span>
    ${isReturn
      ? '<span class="ts-type ts-return">Return</span>'
      : '<span class="ts-type ts-oneway">One way</span>'}
  `;
}

/* ── Date formatting ── */
function formatDateShort(dateStr) {
  const [, m, d] = dateStr.split('-').map(Number);
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${d} ${months[m - 1]}`;
}

function formatDateLong(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  const days   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${days[dt.getDay()]} ${d} ${months[m - 1]}`;
}

/* ── Sort / highlight helpers ── */
function parseDuration(d) {
  if (!d || d === '—') return Infinity;
  const m = d.match(/(\d+)h\s*(\d+)m/);
  return m ? parseInt(m[1]) * 60 + parseInt(m[2]) : Infinity;
}

function parseTaxes(t) {
  if (!t || t === '—') return 0;
  const m = t.match(/[\d,]+/);
  return m ? parseInt(m[0].replace(/,/g, '')) : 0;
}

function isDirectFlight(r) {
  const s = (r._flight?.stops || '').toLowerCase().trim();
  return !s || s === '0' || s === '0 stops' || s === 'non-stop' || s === 'direct' || s === '—';
}

function defaultCompare(a, b) {
  if (a.available !== b.available) return a.available ? -1 : 1;
  const aDirect = isDirectFlight(a) ? 0 : 1;
  const bDirect = isDirectFlight(b) ? 0 : 1;
  if (aDirect !== bDirect) return aDirect - bDirect;
  const aM = a._flight?.miles ?? Infinity;
  const bM = b._flight?.miles ?? Infinity;
  if (aM !== bM) return aM - bM;
  const aT = parseTaxes(a._flight?.taxes);
  const bT = parseTaxes(b._flight?.taxes);
  if (aT !== bT) return aT - bT;
  return parseDuration(a._flight?.duration) - parseDuration(b._flight?.duration);
}

function makeKey(leg, date, cabin, dest, f) {
  const fns = f?.flight_numbers?.join(',') || 'none';
  return `${leg}_${date}_${cabin}_${dest}_${fns}`;
}

function expandResult(r) {
  const outbound = r.flights && r.flights.length > 0
    ? r.flights.map(f => ({
        ...r, _flight: f, _leg: 'outbound', available: f.available ?? false,
        _key: makeKey('out', r.departure_date, r.cabin_class, r.destination, f),
      }))
    : [{ ...r, _flight: null, _leg: 'outbound',
         _key: makeKey('out', r.departure_date, r.cabin_class, r.destination, null) }];

  const retDate = r.return_date || r.departure_date;
  const inbound = r.inbound_flights && r.inbound_flights.length > 0
    ? r.inbound_flights.map(f => ({
        ...r, _flight: f, _leg: 'inbound', available: f.available ?? false,
        departure_date: retDate, return_date: null,
        _key: makeKey('in', retDate, r.cabin_class, r.destination, f),
      }))
    : [];

  return [...outbound, ...inbound];
}

function findGroupBest(flights) {
  const avail = flights.filter(r => r.available && r._flight?.miles != null);
  if (!avail.length) return null;
  return [...avail].sort((a, b) => {
    const aDirect = isDirectFlight(a) ? 0 : 1;
    const bDirect = isDirectFlight(b) ? 0 : 1;
    if (aDirect !== bDirect) return aDirect - bDirect;
    const aM = a._flight.miles, bM = b._flight.miles;
    if (aM !== bM) return aM - bM;
    const aT = parseTaxes(a._flight?.taxes), bT = parseTaxes(b._flight?.taxes);
    if (aT !== bT) return aT - bT;
    return parseDuration(a._flight?.duration) - parseDuration(b._flight?.duration);
  })[0];
}

/* ── Selection helpers ── */
function autoSelectBest(rows) {
  const CABIN_PRIORITY = ['first', 'business', 'premium_economy', 'economy'];
  const searched = currentPayload?.cabin_classes || [];
  const avail = rows.filter(r => r.available && r._flight?.miles != null && searched.includes(r.cabin_class));
  if (!avail.length) return null;
  return [...avail].sort((a, b) => {
    // 1. Direct flight beats any connecting flight
    const aDirect = isDirectFlight(a) ? 0 : 1;
    const bDirect = isDirectFlight(b) ? 0 : 1;
    if (aDirect !== bDirect) return aDirect - bDirect;
    // 2. Highest cabin class
    const aCabin = CABIN_PRIORITY.indexOf(a.cabin_class);
    const bCabin = CABIN_PRIORITY.indexOf(b.cabin_class);
    if (aCabin !== bCabin) return aCabin - bCabin;
    // 3. Lowest miles
    if (a._flight.miles !== b._flight.miles) return a._flight.miles - b._flight.miles;
    // 4. Lowest taxes
    const aT = parseTaxes(a._flight?.taxes), bT = parseTaxes(b._flight?.taxes);
    if (aT !== bT) return aT - bT;
    // 5. Shortest duration
    const aDur = parseDuration(a._flight?.duration), bDur = parseDuration(b._flight?.duration);
    if (aDur !== bDur) return aDur - bDur;
    // 6. Earliest date
    return a.departure_date.localeCompare(b.departure_date);
  })[0];
}

function getMinReturnDate(outboundRow) {
  if (!outboundRow || !currentPayload?.min_nights) return null;
  const d = new Date(outboundRow.departure_date + 'T00:00:00');
  d.setDate(d.getDate() + currentPayload.min_nights);
  return d;
}

function isFlightSelected(r) {
  return r._leg === 'outbound' ? r._key === selectedOutboundKey : r._key === selectedInboundKey;
}

/* ── Render flights ── */
availableOnly.addEventListener('change', renderFlights);

function renderFlights() {
  const onlyAvail = availableOnly.checked;
  const all      = allResults.flatMap(expandResult);
  const outbound = all.filter(r => r._leg === 'outbound');
  const inbound  = all.filter(r => r._leg === 'inbound');

  // Auto-select outbound if none chosen yet
  if (!selectedOutboundKey) {
    const best = autoSelectBest(outbound);
    selectedOutboundKey = best?._key || null;
  }

  // Auto-select inbound if none chosen yet (or reset after outbound change)
  if (currentPayload?.trip_type === 'return' && !selectedInboundKey) {
    const minRetDate = getMinReturnDate(outbound.find(r => r._key === selectedOutboundKey));
    const validInbound = minRetDate
      ? inbound.filter(r => new Date(r.departure_date + 'T00:00:00') >= minRetDate)
      : inbound;
    const best = autoSelectBest(validInbound);
    selectedInboundKey = best?._key || null;
  }

  selectedOutboundRow = outbound.find(r => r._key === selectedOutboundKey) || null;
  selectedInboundRow  = inbound.find(r => r._key === selectedInboundKey)  || null;

  renderItinerary();

  const minRetDate = getMinReturnDate(selectedOutboundRow);
  renderColumn('outboundResults', 'outboundCount', outbound, onlyAvail, null);
  renderColumn('inboundResults',  'inboundCount',  inbound,  onlyAvail, minRetDate);

  updateSummary(outbound);
}

function renderColumn(containerId, countId, rows, onlyAvail, minReturnDate) {
  const container = document.getElementById(containerId);
  const countEl   = document.getElementById(countId);
  if (!container) return;

  const filtered   = onlyAvail ? rows.filter(r => r.available) : rows;
  const totalAvail = rows.filter(r => r.available).length;
  countEl.textContent = totalAvail > 0 ? `${totalAvail} available` : '';

  if (!filtered.length) {
    container.innerHTML = '<div class="no-results-msg">No results yet…</div>';
    return;
  }

  // Group by date
  const dateGroups = {};
  for (const r of filtered) {
    (dateGroups[r.departure_date] = dateGroups[r.departure_date] || []).push(r);
  }

  const allDates = Object.keys(dateGroups).sort();

  // Split into valid / too-early dates when minReturnDate is set
  const validDates   = minReturnDate
    ? allDates.filter(d => new Date(d + 'T00:00:00') >= minReturnDate)
    : allDates;
  const invalidDates = minReturnDate
    ? allDates.filter(d => new Date(d + 'T00:00:00') < minReturnDate)
    : [];

  let html = validDates.map(d => renderDateSection(d, dateGroups[d], false)).join('');
  if (invalidDates.length) {
    html += `<div class="date-divider">
      <span class="date-divider-label">Earlier dates (min nights not met)</span>
    </div>`;
    html += invalidDates.map(d => renderDateSection(d, dateGroups[d], true)).join('');
  }

  container.innerHTML = html;

  // Expand toggle listeners
  container.querySelectorAll('.day-expand-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      btn.closest('.day-group').classList.toggle('day-expanded');
    });
  });

  // Card click listeners for selection
  container.querySelectorAll('.flight-card[data-key]').forEach(card => {
    card.addEventListener('click', () => {
      const key = card.dataset.key;
      const leg = card.dataset.leg;
      if (leg === 'outbound') {
        if (selectedOutboundKey !== key) {
          selectedOutboundKey = key;
          selectedInboundKey  = null; // reset so inbound auto-selects for new outbound date
        }
      } else {
        selectedInboundKey = key;
      }
      renderFlights();
    });
  });
}

function renderDateSection(dateStr, rows, dimmed) {
  const CABIN_ORDER = ['economy', 'premium_economy', 'business', 'first'];

  const availCount = rows.filter(r => r.available).length;
  const countLabel = availCount > 0 ? `${availCount} available` : 'No seats';
  const countClass = availCount > 0 ? 'day-count-avail' : 'day-count-none';

  // Group by cabin within this date
  const cabinGroups = {};
  for (const r of rows) {
    (cabinGroups[r.cabin_class] = cabinGroups[r.cabin_class] || []).push(r);
  }

  const cabinSections = CABIN_ORDER
    .filter(c => cabinGroups[c])
    .map(c => renderCabinInDate(c, cabinGroups[c]))
    .join('');

  return `<div class="date-section${dimmed ? ' date-section-dimmed' : ''}">
    <div class="date-header">
      <span class="date-label">${formatDateLong(dateStr)}</span>
      <span class="day-count ${countClass}">${countLabel}</span>
    </div>
    ${cabinSections}
  </div>`;
}

function renderCabinInDate(cabinClass, flights) {
  const CABIN_LABELS = { economy: 'Economy', premium_economy: 'Premium Economy', business: 'Business', first: 'First' };
  const label = CABIN_LABELS[cabinClass] || cabinClass;

  const sorted  = [...flights].sort(defaultCompare);
  const best    = findGroupBest(sorted);
  const primary = best || sorted[0];
  const extras  = sorted.filter(r => r !== primary);

  const availMiles = flights.filter(r => r.available && r._flight?.miles != null).map(r => r._flight.miles);
  const minMiles   = availMiles.length > 0 ? Math.min(...availMiles) : null;

  const expandBtn = extras.length > 0 ? `
    <div class="day-extras">${extras.map(r => renderFlightCard(r, r === best, isFlightSelected(r))).join('')}</div>
    <button class="day-expand-btn" type="button">
      <span class="btn-show-more">+${extras.length} more flight${extras.length !== 1 ? 's' : ''}</span>
      <span class="btn-show-less">Show less</span>
      <svg class="day-expand-arrow" width="12" height="12" viewBox="0 0 12 12" fill="none">
        <path d="M2 4L6 8L10 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>` : '';

  return `<div class="cabin-in-date">
    <div class="cabin-in-date-header">
      <span class="cabin-in-date-name">${label}</span>
      ${minMiles != null ? `<span class="cabin-miles">From ${minMiles.toLocaleString()} mi</span>` : ''}
    </div>
    <div class="day-group">
      ${renderFlightCard(primary, primary === best, isFlightSelected(primary))}
      ${expandBtn}
    </div>
  </div>`;
}

function renderFlightCard(r, isBest, isSelected) {
  const f = r._flight;
  const flightNum = f?.flight_numbers?.length > 0 ? f.flight_numbers.join(' › ') : '—';
  const depTime   = f?.departure_time || '—';
  const arrTime   = f?.arrival_time   || '—';
  const duration  = f?.duration       || '—';
  const stopsRaw  = f?.stops;
  const stopsNum  = stopsRaw != null ? parseInt(stopsRaw, 10) : null;
  const stops     = stopsNum != null && !isNaN(stopsNum)
    ? (stopsNum === 0 ? 'Direct' : `${stopsNum} stop${stopsNum !== 1 ? 's' : ''}`)
    : (stopsRaw || '—');
  const milesStr  = f?.miles != null ? f.miles.toLocaleString() : '—';
  const taxes     = f?.taxes || '—';

  const cabinDisplay = {
    economy: 'Economy', premium_economy: 'Prem. Eco',
    business: 'Business', first: 'First',
  }[r.cabin_class] || r.cabin_class;

  let badgesHtml = '';
  if (r.error && !r.available) {
    const errTip = (r.error || '').replace(/"/g, '&quot;').slice(0, 200);
    badgesHtml = `<span class="badge badge-error" title="${errTip}">Error ⓘ</span>`;
  } else if (r.available) {
    badgesHtml = `<span class="badge badge-available">✓ Available</span>`;
    if (isBest) badgesHtml += ` <span class="badge badge-best">★ Best</span>`;
  } else {
    badgesHtml = `<span class="badge badge-unavailable">No seats</span>`;
  }
  if (r.note) {
    badgesHtml += ` <span class="badge badge-note" title="${r.note.replace(/"/g, '&quot;')}">ℹ Dates adjusted</span>`;
  }

  const cardClass = [
    'flight-card',
    r.available ? 'flight-available' : 'flight-unavail',
    isBest && r.available ? 'flight-best' : '',
    isSelected ? 'flight-selected' : '',
  ].filter(Boolean).join(' ');

  return `<div class="${cardClass}" data-key="${r._key}" data-leg="${r._leg}">
    <div class="fc-main">
      <div class="fc-route">
        <span class="fc-dest">${r.destination}</span>
        <span class="fc-times">
          <span class="fc-dep">${depTime}</span>
          <span class="fc-arrow">→</span>
          <span class="fc-arr">${arrTime}</span>
        </span>
      </div>
      <div class="fc-meta">
        <span class="fc-num">${flightNum}</span>
        <span class="fc-sep">·</span>
        <span class="fc-dur">${duration}</span>
        <span class="fc-sep">·</span>
        <span class="fc-stops">${stops}</span>
        <span class="fc-sep">·</span>
        <span class="fc-cabin">${cabinDisplay}</span>
      </div>
    </div>
    <div class="fc-aside">
      <div class="fc-miles">${milesStr}<span class="fc-mi-label"> mi</span></div>
      <div class="fc-taxes">${taxes}</div>
      <div class="fc-badges">${badgesHtml}</div>
    </div>
  </div>`;
}

function sumTaxes(t1, t2) {
  const parse = t => {
    if (!t || t === '—') return null;
    const m = t.match(/^([A-Z]+)\s*([\d,]+)/);
    return m ? { currency: m[1], amount: parseInt(m[2].replace(/,/g, '')) } : null;
  };
  const p1 = parse(t1), p2 = parse(t2);
  if (p1 && p2 && p1.currency === p2.currency) {
    return `${p1.currency} ${(p1.amount + p2.amount).toLocaleString()}`;
  }
  if (p1 && p2) return `${t1} + ${t2}`;
  return t1 || t2 || '—';
}

function renderItinerary() {
  const sec = document.getElementById('itinerarySection');
  if (!sec) return;
  if (!selectedOutboundRow) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');

  const isReturn = currentPayload?.trip_type === 'return';
  const origin   = currentPayload?.origin || '';

  const renderLeg = (row, direction) => {
    const f = row._flight;
    if (!f) return '';
    const flightNum  = f.flight_numbers?.join(' › ') || '—';
    const depTime    = f.departure_time || '—';
    const arrTime    = f.arrival_time   || '—';
    const duration   = f.duration       || '—';
    const milesStr   = f.miles != null ? f.miles.toLocaleString() : '—';
    const taxes      = f.taxes || '—';
    const cabinLabel = { economy: 'Economy', premium_economy: 'Prem. Eco', business: 'Business', first: 'First' }[row.cabin_class] || row.cabin_class;
    const dateStr    = formatDateLong(row.departure_date);
    return `<div class="it-flight">
      <div class="it-flight-left">
        <span class="it-direction">${direction}</span>
        <span class="it-date">${dateStr}</span>
        <span class="it-times">${depTime} → ${arrTime}</span>
        <span class="it-meta">${flightNum} · ${duration} · ${cabinLabel}</span>
      </div>
      <div class="it-flight-right">
        <span class="it-miles">${milesStr}<span class="it-mi-label"> mi</span></span>
        <span class="it-taxes">${taxes}</span>
      </div>
    </div>`;
  };

  const dest = selectedOutboundRow.destination;

  let totalHtml = '';
  if (isReturn && selectedInboundRow) {
    const outM  = selectedOutboundRow._flight?.miles ?? 0;
    const inM   = selectedInboundRow._flight?.miles  ?? 0;
    const total = outM + inM;
    const totalTax = sumTaxes(
      selectedOutboundRow._flight?.taxes,
      selectedInboundRow._flight?.taxes
    );
    totalHtml = `<div class="it-total">
      <span class="it-total-label">Total</span>
      <div class="it-total-values">
        <span class="it-total-miles">${outM.toLocaleString()} + ${inM.toLocaleString()} = <strong>${total.toLocaleString()} mi</strong></span>
        <span class="it-total-taxes">${totalTax} taxes &amp; surcharges</span>
      </div>
    </div>`;
  }

  const warnHtml = !selectedOutboundRow.available
    ? '<span class="it-warn">Selected flight unavailable</span>' : '';

  sec.innerHTML = `
    <div class="it-header">
      <span class="it-title">Selected Itinerary</span>
      ${warnHtml}
    </div>
    <div class="it-flights">
      ${renderLeg(selectedOutboundRow, `${origin} → ${dest}`)}
      ${isReturn && selectedInboundRow
          ? renderLeg(selectedInboundRow, `${dest} → ${origin}`)
          : isReturn ? '<div class="it-empty">Return — select a flight from the results below</div>' : ''}
    </div>
    ${totalHtml}
  `;
}

function updateSummary(outboundRows) {
  const available = (outboundRows || []).filter(r => r.available).length;
  const total     = (outboundRows || []).length;
  resultsSummary.textContent = `${available} of ${total} outbound flights have award seats`;
}

/* ── CSV export ── */
exportBtn.addEventListener('click', () => {
  const headers = ['Origin','Destination','Date','Return','Flight','Dep','Duration','Arr','Stops','Cabin','Miles','Taxes','Available'];
  const rows = allResults.flatMap(expandResult).map(r => {
    const f = r._flight;
    return [
      r.origin, r.destination, r.departure_date, r.return_date || '',
      f?.flight_numbers?.join('>') || '',
      f?.departure_time || '', f?.duration || '', f?.arrival_time || '',
      f?.stops || '',
      r.cabin_class,
      f?.miles ?? '',
      f?.taxes  || '',
      r.available ? 'Yes' : 'No',
    ];
  });

  const csv = [headers, ...rows]
    .map(row => row.map(v => `"${String(v).replace(/"/g, '""')}"`).join(','))
    .join('\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = `award-results-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
});

/* ── Custom date picker ── */
function createDatePicker(wrapperId, hiddenId) {
  const wrap   = document.getElementById(wrapperId);
  const hidden = document.getElementById(hiddenId);
  const disp   = wrap.querySelector('.date-picker-display');
  const cal    = wrap.querySelector('.date-picker-cal');

  const MONTHS  = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const M_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const DAY_ABB = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

  let viewYear  = new Date().getFullYear();
  let viewMonth = new Date().getMonth();
  let selected  = null;
  let minDateVal = null;

  function toIso(d) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  }
  function fmtDisp(d) {
    return `${DAY_ABB[d.getDay()]}, ${d.getDate()} ${M_SHORT[d.getMonth()]} ${d.getFullYear()}`;
  }

  function renderCal() {
    const today    = new Date(); today.setHours(0,0,0,0);
    const firstDay = new Date(viewYear, viewMonth, 1).getDay();
    const daysInMo = new Date(viewYear, viewMonth + 1, 0).getDate();

    const atMin = minDateVal &&
      (viewYear < minDateVal.getFullYear() ||
       (viewYear === minDateVal.getFullYear() && viewMonth <= minDateVal.getMonth()));

    let html = `<div class="dp-header">
      <button type="button" class="dp-nav" data-dir="-1" ${atMin ? 'disabled style="opacity:.3;cursor:default"' : ''}>‹</button>
      <span class="dp-month-year">${MONTHS[viewMonth]} ${viewYear}</span>
      <button type="button" class="dp-nav" data-dir="1">›</button>
    </div><div class="dp-grid">
      <span class="dp-dow">Su</span><span class="dp-dow">Mo</span><span class="dp-dow">Tu</span>
      <span class="dp-dow">We</span><span class="dp-dow">Th</span><span class="dp-dow">Fr</span>
      <span class="dp-dow">Sa</span>`;

    for (let i = 0; i < firstDay; i++) html += '<span></span>';
    for (let d = 1; d <= daysInMo; d++) {
      const dt        = new Date(viewYear, viewMonth, d);
      const beforeMin = minDateVal && dt < minDateVal;
      const disabled  = dt < today || beforeMin;
      const sel       = selected && dt.getTime() === selected.getTime();
      const cls       = ['dp-day', disabled ? 'dp-past' : '', sel ? 'dp-selected' : ''].filter(Boolean).join(' ');
      html += `<button type="button" class="${cls}" data-val="${toIso(dt)}" ${disabled ? 'disabled' : ''}>${d}</button>`;
    }
    html += '</div>';
    cal.innerHTML = html;

    cal.querySelectorAll('.dp-nav:not([disabled])').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        viewMonth += parseInt(btn.dataset.dir);
        if (viewMonth > 11) { viewMonth = 0; viewYear++; }
        if (viewMonth < 0)  { viewMonth = 11; viewYear--; }
        renderCal();
      });
    });

    cal.querySelectorAll('.dp-day:not([disabled])').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        selected = new Date(btn.dataset.val + 'T00:00:00');
        hidden.value = btn.dataset.val;
        disp.textContent = fmtDisp(selected);
        disp.classList.add('dp-has-value');
        cal.classList.add('hidden');
        hidden.dispatchEvent(new Event('change', { bubbles: false }));
        updateEstimate();
      });
    });
  }

  function openCal() {
    document.querySelectorAll('.date-picker-cal').forEach(c => { if (c !== cal) c.classList.add('hidden'); });
    if (selected) {
      viewYear = selected.getFullYear(); viewMonth = selected.getMonth();
    } else if (minDateVal) {
      viewYear = minDateVal.getFullYear(); viewMonth = minDateVal.getMonth();
    } else {
      const now = new Date(); viewYear = now.getFullYear(); viewMonth = now.getMonth();
    }
    renderCal();
    cal.classList.remove('hidden');
  }

  disp.addEventListener('click', e => {
    e.stopPropagation();
    cal.classList.contains('hidden') ? openCal() : cal.classList.add('hidden');
  });
  disp.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openCal(); }
  });
  document.addEventListener('click', e => {
    if (!wrap.contains(e.target)) cal.classList.add('hidden');
  });

  return {
    getValue: () => hidden.value,
    setValue(dateStr) {
      hidden.value = dateStr;
      selected   = new Date(dateStr + 'T00:00:00');
      viewYear   = selected.getFullYear();
      viewMonth  = selected.getMonth();
      disp.textContent = fmtDisp(selected);
      disp.classList.add('dp-has-value');
    },
    clearValue() {
      hidden.value = '';
      selected = null;
      disp.textContent = 'Select date';
      disp.classList.remove('dp-has-value');
    },
    setMinDate(dateStr) {
      minDateVal = dateStr ? new Date(dateStr + 'T00:00:00') : null;
    },
  };
}

/* ── Origin single-select airport widget ── */
function initOriginWidget() {
  const wrap     = document.getElementById('originWrap');
  const tagsRow  = document.getElementById('originTagsRow');
  const search   = document.getElementById('originSearch');
  const dropdown = document.getElementById('originDropdown');
  const hidden   = document.getElementById('origin');

  let selectedCode = hidden.value || '';

  function setOrigin(code) {
    selectedCode = code;
    hidden.value = code;
    search.value = '';
    renderTag();
    dropdown.classList.add('hidden');
    hidden.dispatchEvent(new Event('change', { bubbles: true }));
    updateEstimate();
  }

  function renderTag() {
    tagsRow.querySelectorAll('.airport-tag').forEach(t => t.remove());
    if (!selectedCode) return;
    const tag = document.createElement('span');
    tag.className = 'airport-tag';
    tag.innerHTML = `${selectedCode} <span class="rm" title="Clear">×</span>`;
    tag.querySelector('.rm').addEventListener('click', e => {
      e.stopPropagation();
      selectedCode = '';
      hidden.value = '';
      renderTag();
      updateEstimate();
    });
    tagsRow.insertBefore(tag, search);
  }

  function renderOriginDropdown(query) {
    const q = query.toLowerCase();
    const matched = q
      ? allAirports.filter(a => a.code.toLowerCase().includes(q) || a.name.toLowerCase().includes(q))
      : allAirports;
    if (!matched.length) { dropdown.classList.add('hidden'); return; }
    dropdown.innerHTML = matched.slice(0, 40).map(a =>
      `<div class="airport-opt" data-code="${a.code}">
        <span class="ap-code">${a.code}</span>
        <span class="ap-name">${a.name}</span>
      </div>`
    ).join('');
    dropdown.querySelectorAll('.airport-opt').forEach(opt => {
      opt.addEventListener('click', () => setOrigin(opt.dataset.code));
      opt.addEventListener('mouseenter', () => {
        dropdown.querySelectorAll('.airport-opt').forEach(o => o.classList.remove('active'));
        opt.classList.add('active');
      });
    });
    dropdown.classList.remove('hidden');
  }

  search.addEventListener('input',   () => renderOriginDropdown(search.value.trim()));
  search.addEventListener('focus',   () => renderOriginDropdown(search.value.trim()));
  search.addEventListener('keydown', e => {
    if (e.key === 'Escape') dropdown.classList.add('hidden');
    if (e.key === 'Enter')  { e.preventDefault(); dropdown.querySelector('.airport-opt.active')?.click(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const opts = [...dropdown.querySelectorAll('.airport-opt')];
      const idx  = opts.findIndex(o => o.classList.contains('active'));
      const next = e.key === 'ArrowDown' ? opts[idx+1] ?? opts[0] : opts[idx-1] ?? opts.at(-1);
      opts.forEach(o => o.classList.remove('active'));
      next?.classList.add('active');
      next?.scrollIntoView({ block: 'nearest' });
    }
  });

  document.addEventListener('click', e => {
    if (!wrap.contains(e.target)) dropdown.classList.add('hidden');
  });
  tagsRow.addEventListener('click', e => {
    if (e.target === tagsRow || e.target === wrap) search.focus();
  });

  renderTag();
}

/* ── Initialise ── */
(async function init() {
  const today = new Date();
  const pad = n => String(n).padStart(2, '0');
  const fmt = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;

  const start = new Date(today); start.setDate(start.getDate() + 3);

  const depStartPicker = createDatePicker('depStartWrap', 'depStart');
  const depEndPicker   = createDatePicker('depEndWrap',   'depEnd');
  depStartPicker.setValue(fmt(start));

  document.getElementById('depStart').addEventListener('change', () => {
    const startVal = document.getElementById('depStart').value;
    depEndPicker.setMinDate(startVal);
    const endVal = document.getElementById('depEnd').value;
    if (endVal && endVal <= startVal) depEndPicker.clearValue();
    updateEstimate();
  });

  document.querySelector('.return-only').classList.add('hidden-field');

  await loadAirports();

  updateEstimate();
})();
