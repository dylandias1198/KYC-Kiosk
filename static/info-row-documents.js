/* Document entries and viewer URLs/metadata summaries come exclusively from all_doc_type, all_s3_url, and all_other_metadata. */
function associatedDocumentKind(doc) {
  const text = `${doc.document_type || ''} ${doc.url || ''}`.toLowerCase();
  if (/\.(?:pdf)(?:$|[?#])/.test(text)) return 'pdf';
  if (/\.(?:png|jpe?g|webp|gif|bmp|svg)(?:$|[?#])/.test(text)) return 'image';
  if (/\.(?:txt|text|csv|json|log|xml)(?:$|[?#])/.test(text) || /\b(?:text|plain text|csv|json|log|xml)\b/.test(text)) return 'text';
  return 'link';
}
function associatedMetadataSummary(doc) {
  const fields = Array.isArray(doc.metadata_fields) ? doc.metadata_fields : [];
  if (!fields.length) return '<span class="metadata-empty">No metadata summary available</span>';
  return `<ul class="associated-metadata-list">${fields.map(field => `<li>${escapeHtml(field)}</li>`).join('')}</ul>`;
}
function associatedDocumentStatus(doc) {
  return hasSuppliedValue(doc.status) ? String(doc.status) : (safeHttpUrl(doc.url) ? 'Available' : 'Link unavailable');
}
function renderAssociatedDocumentMarkup(associatedDocuments, recordIndex) {
  const docs = Array.isArray(associatedDocuments) ? associatedDocuments : [];
  const selectedIds = Array.isArray(selectedAssociatedDocumentIds) ? selectedAssociatedDocumentIds : [null, null];
  const selectedCount = selectedIds.filter(id => id !== null && id !== undefined).length;
  const choices = docs.length ? `<div class="associated-document-grid" role="list" aria-label="Available associated documents">${docs.map(doc => {
    const id = Number(doc.document_id);
    const label = hasSuppliedValue(doc.document_type) ? String(doc.document_type) : 'Document';
    const status = associatedDocumentStatus(doc);
    const state = /available|conversion required/i.test(status) ? 'ready' : /unavailable|unsupported|failed/i.test(status) ? 'warning' : 'neutral';
    const slot = selectedIds.findIndex(selectedId => selectedId !== null && selectedId !== undefined && Number(selectedId) === id);
    const checked = slot >= 0;
    const slotClass = slot === 0 ? ' is-slot-one' : slot === 1 ? ' is-slot-two' : '';
    const mapping = slot >= 0 ? `<span class="document-slot-map">Selected · Slot ${slot + 1}</span>` : '';
    const subtitle = (Array.isArray(doc.metadata_fields) && doc.metadata_fields.length)
      ? `Source: all_doc_type · Metadata: ${doc.metadata_fields.join(', ')}`
      : 'Source: all_doc_type · Metadata: none supplied';
    const conversion = doc.conversion_required ? '<span class="associated-conversion-note">Local conversion required</span>' : '';
    return `<article class="associated-document-card${checked ? ' is-selected is-active' : ''}${slotClass}" role="listitem" data-associated-document="${id}">
      <label class="associated-document-choice">
        <input type="checkbox" class="associated-document-checkbox" data-document-select="${id}" aria-label="Select ${escapeHtml(label)} for comparison"${checked ? ' checked' : ''}>
        <span class="associated-document-copy"><span class="all-document-kicker">DOCUMENT</span><span class="associated-document-type">${escapeHtml(label)}</span><span class="associated-document-subtitle">${escapeHtml(subtitle)}</span>${conversion}${mapping}</span>
        <span class="all-document-status" data-state="${state}">${escapeHtml(status)}</span>
      </label>
    </article>`;
  }).join('')}</div>` : '<div class="associated-document-empty" role="status"><strong>No associated documents available</strong><span>This user has no document types in all_doc_type.</span></div>';
  const slots = [0, 1].map(slot => {
    const id = selectedIds[slot];
    const doc = id === null || id === undefined ? null : docs.find(entry => Number(entry.document_id) === Number(id));
    const number = slot + 1;
    const title = doc ? String(doc.document_type || 'Document') : `Choose a document for slot ${number}`;
    const remove = doc ? `<button type="button" class="slot-clear" data-compare-clear="${slot}" aria-label="Remove comparison slot ${number}">Remove</button>` : '';
    const replacementOptions = doc ? docs.filter(entry => Number(entry.document_id) === Number(id) || Number(selectedIds[1 - slot]) !== Number(entry.document_id)).map(entry => `<option value="${Number(entry.document_id)}"${Number(entry.document_id) === Number(id) ? ' selected' : ''}>${escapeHtml(entry.document_type || 'Document')}</option>`).join('') : '';
    const replacement = doc ? `<label class="slot-replacement-label">Replace<select class="slot-replacement" data-slot-replace="${slot}" aria-label="Replace document in comparison slot ${number}">${replacementOptions}</select></label>` : '';
    const metadata = doc ? `<div class="comparison-slot-metadata"><span class="metadata-label">Metadata summary</span>${associatedMetadataSummary(doc)}</div>` : '';
    const body = doc ? `${metadata}<div class="comparison-viewer-wrap" data-comparison-viewer="${slot}" data-document-id="${Number(doc.document_id)}"><div class="comparison-viewer-state" data-viewer-state="${slot}" role="status">${doc.conversion_required ? 'Conversion required — loading selected document.' : safeHttpUrl(doc.url) ? 'Loading selected document…' : 'Document link is missing or invalid.'}</div><div class="comparison-viewer-content" data-viewer-content="${slot}"></div></div>` : `<div class="comparison-empty" role="status"><span class="comparison-empty-icon">${number}</span><strong>Empty comparison slot</strong><span>Choose any available document above.</span></div>`;
    const load = doc ? `<button type="button" class="all-document-action" data-compare-load="${slot}"${!doc.conversion_required && !safeHttpUrl(doc.url) ? ' disabled' : ''}>Reload preview</button>` : '';
    const openHref = doc ? (doc.conversion_required ? `/api/records/${recordIndex}/documents/${Number(doc.document_id)}` : safeHttpUrl(doc.url)) : '';
    const open = openHref ? `<a class="all-document-action secondary" href="${escapeHtml(openHref)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">Open in new tab</a>` : '';
    const kind = doc ? associatedDocumentKind(doc) : '';
    return `<article class="comparison-slot${doc ? ' is-filled' : ''}" data-comparison-slot="${slot}" aria-label="Comparison slot ${number}"><header class="comparison-slot-header"><div><span class="all-document-kicker">COMPARISON ${number}</span><h4>${escapeHtml(title)}</h4>${doc ? `<span class="comparison-document-type">${escapeHtml(kind.toUpperCase())} · ${escapeHtml(associatedDocumentStatus(doc))}</span>` : ''}</div><div class="comparison-slot-header-actions">${replacement}${remove}</div></header><div class="comparison-slot-actions">${load}${open}</div>${body}</article>`;
  }).join('');
  return `<section class="associated-document-library" aria-label="Associated document selection"><div class="comparison-section-heading"><div><span class="eyebrow">DOCUMENT LIBRARY</span><h4>Documents</h4><p>Select two documents to keep a side-by-side comparison open. The active document is highlighted.</p></div><div class="document-counts"><span class="comparison-count">${docs.length} available</span><span class="selected-count" data-selected-count>${selectedCount} of 2 selected</span></div></div><div class="selection-message" data-selection-message role="status" aria-live="polite">${escapeHtml(typeof associatedSelectionMessage === 'undefined' ? '' : associatedSelectionMessage)}</div>${choices}</section><section class="associated-document-comparison" aria-label="Two-document comparison">${slots}</section>`;
}
if (typeof window !== 'undefined') window.renderInfoRowAssociatedDocuments = renderAssociatedDocumentMarkup;

var selectedAssociatedDocuments = [];
var selectedAssociatedDocumentIds = [null, null];
var associatedSelectionMessage = '';
var associatedDocumentsUserId = null;
var associatedViewerBlobUrls = {};
var associatedViewerRequestIds = [0, 0];
function rerenderAssociatedDocuments() {
  const content = document.getElementById('tabContent');
  if (content && window.kioskRenderDocuments) content.innerHTML = window.kioskRenderDocuments();
}
function reloadSelectedAssociatedComparisons() {
  selectedAssociatedDocumentIds.forEach((id, slot) => {
    if (id !== null && id !== undefined) loadAssociatedComparison(slot);
  });
}
function releaseAssociatedViewer(slot, invalidate = true) {
  const previous = associatedViewerBlobUrls[slot];
  if (invalidate) associatedViewerRequestIds[slot] += 1;
  if (previous) URL.revokeObjectURL(previous);
  delete associatedViewerBlobUrls[slot];
}
async function loadAssociatedComparison(slot) {
  const id = selectedAssociatedDocumentIds[slot];
  const doc = selectedAssociatedDocuments.find(entry => Number(entry.document_id) === Number(id));
  const state = document.querySelector(`[data-viewer-state="${slot}"]`);
  const content = document.querySelector(`[data-viewer-content="${slot}"]`);
  const wrapper = document.querySelector(`[data-comparison-viewer="${slot}"]`);
  if (!doc || !state || !content || !wrapper) return;
  const requestId = ++associatedViewerRequestIds[slot];
  const documentId = Number(doc.document_id);
  const source = safeHttpUrl(doc.url);
  if (!doc.conversion_required && !source) { state.textContent = 'Document link is missing or invalid.'; state.classList.add('error'); return; }
  state.classList.remove('error'); state.textContent = doc.conversion_required ? 'Converting selected document…' : 'Loading selected document…';
  content.replaceChildren();
  try {
    let viewUrl = source;
    let contentType = '';
    if (doc.conversion_required) {
      const response = await fetch(`/api/records/${selectedIndex}/documents/${Number(doc.document_id)}`, {headers: {Accept: 'application/pdf, image/jpeg, image/png, image/webp'}});
      if (!response.ok) {
        let message = `Document unavailable (HTTP ${response.status})`;
        try { const payload = await response.json(); if (payload.error) message = payload.error; } catch (_) {}
        throw new Error(message);
      }
      contentType = String(response.headers.get('Content-Type') || '').split(';')[0].toLowerCase();
      if (!['application/pdf', 'image/jpeg', 'image/png', 'image/webp'].includes(contentType)) throw new Error('Converted document has an unsupported content type.');
      const blob = await response.blob();
      if (requestId !== associatedViewerRequestIds[slot] || Number(selectedAssociatedDocumentIds[slot]) !== documentId) return;
      releaseAssociatedViewer(slot, false);
      viewUrl = URL.createObjectURL(blob);
      associatedViewerBlobUrls[slot] = viewUrl;
    }
    if (doc.conversion_required) {
      if (contentType === 'application/pdf') content.innerHTML = `<iframe class="all-document-frame" title="${escapeHtml(doc.document_type)} preview" src="${escapeHtml(viewUrl)}#page=1" referrerpolicy="no-referrer"></iframe>`;
      else content.innerHTML = `<img class="all-document-image" src="${escapeHtml(viewUrl)}" alt="${escapeHtml(doc.document_type)} preview">`;
    } else {
      const kind = associatedDocumentKind(doc);
      if (kind === 'image') content.innerHTML = `<img class="all-document-image" src="${escapeHtml(viewUrl)}" alt="${escapeHtml(doc.document_type)} preview" referrerpolicy="no-referrer">`;
      else if (kind === 'pdf') content.innerHTML = `<iframe class="all-document-frame" title="${escapeHtml(doc.document_type)} PDF preview" src="${escapeHtml(viewUrl)}#page=1" referrerpolicy="no-referrer"></iframe>`;
      else if (kind === 'text') content.innerHTML = `<iframe class="all-document-frame plain-document-frame" sandbox="" title="${escapeHtml(doc.document_type)} text preview" src="${escapeHtml(viewUrl)}" referrerpolicy="no-referrer"></iframe>`;
      else content.innerHTML = `<iframe class="all-document-frame plain-document-frame" sandbox="" title="${escapeHtml(doc.document_type)} linked preview" src="${escapeHtml(viewUrl)}" referrerpolicy="no-referrer"></iframe>`;
    }
    if (requestId !== associatedViewerRequestIds[slot] || Number(selectedAssociatedDocumentIds[slot]) !== documentId) { content.replaceChildren(); return; }
    const media = content.querySelector('img,iframe');
    if (!media) throw new Error('Document preview could not be created.');
    media.addEventListener('load', () => { state.textContent = 'Preview loaded'; }, {once: true});
    media.addEventListener('error', () => { state.textContent = 'Document preview failed to load.'; state.classList.add('error'); }, {once: true});
    state.textContent = 'Preview requested';
  } catch (error) {
    if (requestId !== associatedViewerRequestIds[slot] || Number(selectedAssociatedDocumentIds[slot]) !== documentId) return;
    content.replaceChildren(); state.textContent = error.message || 'Document could not be loaded.'; state.classList.add('error');
  }
}
function updateAssociatedDocumentSelection(id, checked) {
  const numericId = Number(id);
  const currentSlot = selectedAssociatedDocumentIds.findIndex(selectedId => selectedId !== null && selectedId !== undefined && Number(selectedId) === numericId);
  if (checked) {
    if (currentSlot >= 0) return;
    const slot = selectedAssociatedDocumentIds.findIndex(selectedId => selectedId === null || selectedId === undefined);
    if (slot < 0) {
      associatedSelectionMessage = 'You can select up to two documents. Uncheck a selected document before choosing another.';
      rerenderAssociatedDocuments();
      reloadSelectedAssociatedComparisons();
      return;
    }
    if (!selectedAssociatedDocuments.some(doc => Number(doc.document_id) === numericId)) return;
    releaseAssociatedViewer(slot);
    selectedAssociatedDocumentIds[slot] = numericId;
    associatedSelectionMessage = '';
    rerenderAssociatedDocuments();
    reloadSelectedAssociatedComparisons();
    return;
  }
  if (currentSlot < 0) return;
  releaseAssociatedViewer(currentSlot);
  const remaining = selectedAssociatedDocumentIds.filter(selectedId => selectedId !== null && selectedId !== undefined && Number(selectedId) !== numericId);
  [0, 1].forEach(slot => releaseAssociatedViewer(slot));
  selectedAssociatedDocumentIds = [remaining[0] ?? null, remaining[1] ?? null];
  associatedSelectionMessage = '';
  rerenderAssociatedDocuments();
  reloadSelectedAssociatedComparisons();
}

document.addEventListener('change', event => {
  const checkbox = event.target.closest('input[data-document-select]');
  if (checkbox) { updateAssociatedDocumentSelection(checkbox.dataset.documentSelect, Boolean(checkbox.checked)); return; }
  const replacement = event.target.closest('select[data-slot-replace]');
  if (!replacement || !replacement.value) return;
  const slot = Number(replacement.dataset.slotReplace); const id = Number(replacement.value);
  if (![0, 1].includes(slot) || Number(selectedAssociatedDocumentIds[1 - slot]) === id || !selectedAssociatedDocuments.some(doc => Number(doc.document_id) === id)) return;
  releaseAssociatedViewer(slot);
  selectedAssociatedDocumentIds[slot] = id;
  associatedSelectionMessage = '';
  rerenderAssociatedDocuments();
  reloadSelectedAssociatedComparisons();
});
document.addEventListener('click', event => {
  const clear = event.target.closest('[data-compare-clear]');
  if (clear) {
    const slot = Number(clear.dataset.compareClear);
    if (![0, 1].includes(slot)) return;
    const id = selectedAssociatedDocumentIds[slot];
    if (id !== null && id !== undefined) updateAssociatedDocumentSelection(id, false);
    return;
  }
  const load = event.target.closest('[data-compare-load]');
  if (load) { loadAssociatedComparison(Number(load.dataset.compareLoad)); }
});
if (typeof window !== 'undefined') window.kioskViewAssociatedDocument = loadAssociatedComparison;
