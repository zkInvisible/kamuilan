'use strict';

const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const normalize = value => String(value ?? '').toLocaleLowerCase('tr-TR').replace(/ı/g, 'i').normalize('NFD').replace(/[\u0300-\u036f]/g, '');
const TAGS = [];
const state = {jobs: [], tags: new Set(), active: null, scanning: false, loading: false};
let toastTimer, detailController, previousFocus;

function jobTags(job) {
  const tags = new Set(Array.isArray(job.tags) ? job.tags : []);
  const corpus = normalize(`${job.title} ${job.institution} ${job.summary}`);
  if (/memur|buro personel|vhki|sekreter/.test(corpus) && !tags.has('MAKINE_MUHENDIS')) tags.add('BANA_UYGUN_DIGER');
  return TAGS.filter(([key]) => tags.has(key));
}
function hasTag(job, key) { return jobTags(job).some(([tag]) => tag === key); }
function selectedTitle(job) { return [...new Set((job.eligible_positions || []).map(row => row.title))].join(' / ') || job.title; }
function toast(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').style.display = 'block';
  toastTimer = setTimeout(() => { $('toast').style.display = 'none'; }, 3500);
}
async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error('Sunucudan yanıt alınamadı.');
  return response.json();
}
function tagHTML(job, clickable = true) {
  return jobTags(job).map(([key, label, style]) => clickable
    ? `<button class="tag ${style}" data-tag="${key}" title="Bu etiketle filtrele">${escapeHTML(label)}</button>`
    : `<span class="tag ${style}">${escapeHTML(label)}</span>`).join('');
}
function renderCategories() {
  const category = (key, label, count, selected) => `<button class="category" data-category="${key}" aria-pressed="${selected}"><span>${label}</span><span class="count">${count}</span></button>`;
  $('categories').innerHTML = category('all', 'Tüm ilanlar', state.jobs.length, state.tags.size === 0)
    + TAGS.map(([key, label]) => category(key, label, state.jobs.filter(job => hasTag(job, key)).length, state.tags.has(key))).join('');
}
function renderStats() {
  $('total').textContent = state.jobs.length;
  $('engineer').textContent = state.jobs.filter(job => hasTag(job, 'MAKINE_MUHENDIS')).length;
  $('memur').textContent = state.jobs.filter(job => hasTag(job, 'BANA_UYGUN_DIGER')).length;
}
function renderPills() {
  const pill = (key, label) => `<button class="filter-pill" data-remove="${escapeHTML(key)}" aria-label="${escapeHTML(label)} filtresini kaldır">${escapeHTML(label)} <span aria-hidden="true">×</span></button>`;
  let html = [...state.tags].map(key => pill(key, TAGS.find(([tag]) => tag === key)?.[1] || key)).join('');
  if ($('source').value !== 'all') html += pill('source', $('source').value);
  if ($('search').value.trim()) html += pill('search', 'Arama: ' + $('search').value.trim());
  $('activeFilters').innerHTML = html || '<span class="filter-help">Soldaki kategorilerden veya ilan etiketlerinden filtre seçebilirsin.</span>';
}
function render() {
  const query = normalize($('search').value.trim()), source = $('source').value;
  const jobs = state.jobs.filter(job =>
    (!query || normalize(`${job.title} ${job.institution} ${job.summary}`).includes(query)) &&
    (source === 'all' || (job.all_sources || [job.source]).includes(source)) &&
    (!state.tags.size || [...state.tags].some(key => hasTag(job, key)))
  );
  if ($('sort').value === 'title') jobs.sort((a, b) => String(a.title).localeCompare(String(b.title), 'tr'));
  else if ($('sort').value === 'deadline') jobs.sort((a, b) => (a.deadline_info?.days_left ?? Infinity) - (b.deadline_info?.days_left ?? Infinity));
  else jobs.sort((a, b) => (Date.parse(b.created_at?.replace(' ', 'T')) || b.id) - (Date.parse(a.created_at?.replace(' ', 'T')) || a.id));
  $('resultCount').textContent = jobs.length;
  $('resultHint').textContent = `${jobs.length} / ${state.jobs.length} ilan${state.tags.size > 1 ? ' · seçili kategorilerden herhangi biri' : ' · profil filtreleri açık'}`;
  $('jobs').innerHTML = jobs.length ? jobs.map(job => {
    const deadline = job.deadline_info || {};
    const cardTitle = selectedTitle(job);
    const quota = job.eligible_quota == null ? '' : '<span class="quota">' + job.eligible_quota + ' uygun kontenjan</span>';
    const conditions = [...new Set((job.extra_conditions || []).map(item => item.label))];
    const conditionHint = conditions.length ? '<div class="condition-hint">Kontrol et: ' + escapeHTML(conditions.join(' · ')) + '</div>' : '';
    return `<article class="job" data-job-id="${job.id}"><div class="job-top"><span class="source">${escapeHTML(job.source || 'Kamu ilanı')}</span>${quota}</div><h3>${escapeHTML(cardTitle || 'Kamu personeli ilanı')}</h3><div class="institution">${escapeHTML(job.institution || '')}</div><div class="job-tags">${tagHTML(job)}</div>${conditionHint}<div class="job-bottom"><div class="deadline">${escapeHTML(deadline.badge_text || 'Son başvuru tarihi kontrol edilmeli')}<span class="date">${escapeHTML(job.published_date || '')}</span></div><button class="details" data-detail="${job.id}">İlanı incele →</button></div></article>`;
  }).join('') : state.jobs.length
    ? '<div class="empty"><strong>Bu filtrelerde ilan bulunamadı</strong><p>Başka bir kategori seçebilir veya filtreleri sıfırlayabilirsin.</p><button class="btn secondary" data-reset>Filtreleri sıfırla</button></div>'
    : `<div class="empty"><strong>${state.scanning ? 'İlanların başvuru şartları kontrol ediliyor' : 'Profiline uyan doğrulanmış güncel ilan bulunamadı'}</strong><p>${state.scanning ? 'Puan türü ve diploma şartı doğrulanan ilanlar tarama tamamlandığında burada görünecek.' : 'P3 dışında puan türü isteyen veya şartları okunamayan ilanlar gösterilmez. Yeni ilanlar 8 saatte bir kontrol edilir.'}</p>${state.scanning ? '' : '<button class="btn secondary" data-start-scan>↻ İlanları güncelle</button>'}</div>`;
  renderCategories(); renderStats(); renderPills();
}
function resetFilters() {
  state.tags.clear();
  $('search').value = ''; $('source').value = 'all'; $('sort').value = 'newest';
  render();
}
function toggleTag(key) {
  state.tags.has(key) ? state.tags.delete(key) : state.tags.add(key);
  render();
}
async function load() {
  if (state.loading) return;
  state.loading = true;
  try {
    const data = await fetchJSON('/api/jobs?limit=400');
    state.jobs = data.jobs || [];
    TAGS.splice(0, TAGS.length, ...(data.categories || []));
    const previousSource = $('source').value;
    const sources = [...new Set(state.jobs.flatMap(job => job.all_sources || [job.source]).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'tr'));
    $('source').innerHTML = '<option value="all">Tüm kaynaklar</option>' + sources.map(source => `<option value="${escapeHTML(source)}">${escapeHTML(source)}</option>`).join('');
    $('source').value = sources.includes(previousSource) ? previousSource : 'all';
    render();
  } catch (error) {
    if (!state.jobs.length) $('jobs').innerHTML = '<div class="empty"><strong>İlanlar yüklenemedi</strong><p>Bağlantıyı kontrol edip yeniden deneyebilirsin.</p><button class="btn secondary" data-retry>Tekrar dene</button></div>';
    else toast('İlan listesi yenilenemedi; son yüklenen ilanlar gösteriliyor.');
  } finally { state.loading = false; }
}

function officialLink(job) {
  // SBB public documents must be opened through their verified PDF endpoint.
  if (job.pdf_url) return job.pdf_url;
  const url = job.web_url || job.url || '';
  return /^https?:\/\//i.test(url) ? url : '#';
}
function showPositions(data) {
  const rows = data.eligible_positions || [];
  $('mPositions').innerHTML = rows.length ? '<h3>Profiline uyan kadrolar</h3><div class="position-table"><table><thead><tr><th>Unvan</th><th>Puan türü</th><th>Taban puan</th><th>Kontenjan</th></tr></thead><tbody>' + rows.map(row => '<tr><td>' + escapeHTML(row.title) + '</td><td>P' + escapeHTML(row.score_type) + '</td><td>' + escapeHTML(row.minimum_score ?? 'Belirsiz') + '</td><td>' + escapeHTML(row.quota ?? 'Belirtilmemiş') + '</td></tr>').join('') + '</tbody></table></div><div class="position-qualifications">' + rows.map(row => '<p><strong>' + escapeHTML(row.title) + ':</strong> ' + escapeHTML(row.qualification) + '</p>').join('') + '</div>' : '';
  const conditions = data.extra_conditions || [];
  $('mConditions').innerHTML = conditions.length ? '<h3>Diğer başvuru şartlarını kontrol et</h3><ul class="conditions">' + conditions.map(item => '<li><strong>' + escapeHTML(item.label) + ':</strong> ' + escapeHTML(item.text) + '</li>').join('') + '</ul>' : '';
}
function showSourceHealth(sources) {
  $('sourceHealth').innerHTML = Object.entries(sources || {}).filter(([name]) => !name.startsWith('__')).map(([name, report]) => '<div class="source-row"><span>' + escapeHTML(name) + '</span><span class="' + (report.status === 'error' ? 'source-error' : '') + '">' + (report.status === 'error' ? 'Erişilemedi' : report.collected + ' ilan' + (report.status === 'partial' ? ' · kısmen' : '')) + '</span></div>').join('') || 'İlk tarama bekleniyor.';
}
async function openDetail(id) {
  const job = state.jobs.find(job => job.id === id);
  if (!job) return;
  detailController?.abort(); detailController = new AbortController();
  state.active = job; previousFocus = document.activeElement;
  $('mSource').textContent = job.source || '';
  $('mTitle').textContent = selectedTitle(job);
  $('mInstitution').textContent = job.institution || '';
  $('mTags').innerHTML = tagHTML(job, false);
  showPositions(job);
  $('mSummary').textContent = '';
  $('mAdvice').textContent = 'Başvuru koşullarını resmî ilandan kontrol et.';
  $('mAdvice').classList.remove('incompatible');
  $('mLink').href = officialLink(job);
  $('mLink').textContent = job.pdf_url ? 'Resmî ilan PDF’sini aç ↗' : 'Resmî ilanı aç ↗';
  $('detailStatus').textContent = 'İlanın başvuru koşulları okunuyor…';
  $('backdrop').classList.add('open'); document.body.classList.add('modal-open'); $('close').focus();
  try {
    const data = await fetchJSON(`/api/jobs/${id}/detail`, {signal: detailController.signal});
    if (state.active?.id !== id || !$('backdrop').classList.contains('open')) return;
    const detail = data.detail || {};
    showPositions(detail);
    $('mSummary').textContent = 'Kaynak ilan birden fazla kadro içerebilir. Burada yalnızca profil kontrolünü geçen kadrolar gösterilir.';
    if (detail.candidate_advice) $('mAdvice').textContent = detail.candidate_advice;
    $('mAdvice').classList.toggle('incompatible', detail.is_suitable === false);
    $('detailStatus').textContent = 'Başvuru şartları için resmî ilanı incele.';
    if (detail.is_suitable === false || detail.requires_experience) await load();
  } catch (error) {
    if (error.name !== 'AbortError') $('detailStatus').textContent = 'Detay metni okunamadı. Resmî ilan bağlantısını açabilirsin.';
  }
}
function closeDetail() {
  detailController?.abort(); $('backdrop').classList.remove('open'); document.body.classList.remove('modal-open');
  state.active = null; if (previousFocus?.isConnected) previousFocus.focus();
}
function setScanning(scanning) {
  const changed = state.scanning !== scanning;
  state.scanning = scanning; $('scan').disabled = scanning;
  $('scan').textContent = scanning ? '↻ İlanlar taranıyor…' : '↻ İlanları güncelle';
  $('scanStatus').classList.toggle('scanning', scanning);
  $('scanStatus').innerHTML = `<span class="status-dot"></span>${scanning ? 'Kaynaklar taranıyor' : 'Sistem aktif · 8 saatte bir tarama'}`;
  if (changed && !state.jobs.length) render();
}
async function updateScanStatus() {
  try {
    const data = await fetchJSON('/api/scan/status');
    showSourceHealth(data.last_scan?.sources);
    const wasScanning = state.scanning;
    setScanning(Boolean(data.is_scanning));
    if (data.last_scan?.finished_at) {
      const date = new Date(data.last_scan.finished_at.replace(' ', 'T'));
      $('lastScan').textContent = 'Son tarama: ' + (Number.isNaN(date.getTime()) ? data.last_scan.finished_at : date.toLocaleString('tr-TR', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}));
    } else $('lastScan').textContent = 'İlk tarama bekleniyor.';
    if (wasScanning && !state.scanning) { await load(); toast(data.last_scan?.status === 'ERROR' ? 'Kaynaklara erişilemedi. Kaynak durumunu kontrol et.' : data.last_scan?.status === 'PARTIAL' ? 'İlanlar güncellendi; bazı kaynaklar okunamadı.' : 'Tarama tamamlandı. İlanlar güncellendi.'); }
  } catch (error) {
    $('scanStatus').textContent = 'Bağlantı kontrol ediliyor';
  } finally { setTimeout(updateScanStatus, state.scanning ? 3000 : 15000); }
}
async function startScan() {
  setScanning(true);
  try {
    const data = await fetchJSON('/api/scan', {method: 'POST'});
    if (data.status === 'busy') toast('Kaynak taraması zaten devam ediyor.');
    else if (data.status === 'cooldown') toast(`Yeni tarama için ${Math.ceil(data.retry_after_seconds / 60)} dakika beklemelisin.`);
    else toast('Kaynak taraması başladı. Sonuçlar otomatik güncellenecek.');
  } catch (error) { setScanning(false); toast('Tarama başlatılamadı. Yeniden deneyebilirsin.'); }
}

$('categories').addEventListener('click', event => {
  const key = event.target.closest('[data-category]')?.dataset.category;
  if (!key) return;
  if (key === 'all') { state.tags.clear(); render(); }
  else toggleTag(key);
});
$('jobs').addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.detail) openDetail(Number(button.dataset.detail));
  else if (button.dataset.tag) toggleTag(button.dataset.tag);
  else if (button.hasAttribute('data-reset')) resetFilters();
  else if (button.hasAttribute('data-retry')) load();
  else if (button.hasAttribute('data-start-scan')) startScan();
});
$('activeFilters').addEventListener('click', event => {
  const key = event.target.closest('[data-remove]')?.dataset.remove;
  if (!key) return;
  if (key === 'source') $('source').value = 'all';
  else if (key === 'search') $('search').value = '';
  else state.tags.delete(key);
  render();
});
document.querySelector('.stats').addEventListener('click', event => {
  const key = event.target.closest('[data-stat]')?.dataset.stat;
  if (!key) return;
  state.tags.clear();
  if (key === 'engineer') state.tags.add('MAKINE_MUHENDIS');
  if (key === 'memur') state.tags.add('BANA_UYGUN_DIGER');
  render();
});
$('search').addEventListener('input', render);
$('source').addEventListener('change', render);
$('sort').addEventListener('change', render);
$('clearFilters').addEventListener('click', resetFilters);
$('scan').addEventListener('click', startScan);
$('close').addEventListener('click', closeDetail);
$('backdrop').addEventListener('click', event => { if (event.target === $('backdrop')) closeDetail(); });
document.addEventListener('keydown', event => {
  if (!$('backdrop').classList.contains('open')) return;
  if (event.key === 'Escape') closeDetail();
  if (event.key === 'Tab') {
    const focusable = [...$('backdrop').querySelectorAll('button:not(:disabled), a[href]')];
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
load(); updateScanStatus(); setInterval(load, 60000);
