/* KFS-only form rendering. Keep this separate so the initial review document stays lightweight. */
function kfsEvidenceTone(value) {
  const text = String(value ?? '').trim().toLowerCase();
  if (!text || /\b(not detected|not available|failed|failure|unavailable|not passed|review)\b/.test(text)) return 'negative';
  if (/\b(detected|pass|passed|available|success|successful)\b/.test(text)) return 'positive';
  return 'neutral';
}
function kfsSourceStatusTone(value) {
  const text = String(value ?? '').trim().toLowerCase();
  if (/\b(failed|failure|review|not available|unavailable|not requested|not passed)\b/.test(text)) return 'failed';
  if (/\b(pass|passed|success|successful|available)\b/.test(text)) return 'success';
  return '';
}
function kfsEvidenceCard(label, value, key, note = '') {
  const displayValue = hasSuppliedValue(value) ? String(value) : 'Not available';
  return `<div class="form-field kfs-evidence-card kfs-evidence-${kfsEvidenceTone(value)}" data-field-key="${escapeHtml(key)}" data-kfs-evidence="${escapeHtml(key)}"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(displayValue)}</dd>${note ? `<small>${escapeHtml(note)}</small>` : ''}</div>`;
}
function kfsComparisonCard(title, sourceLabel, sourceValue, sourceField, kycLabel, kycValue, kycField, status, statusField, scoreValue, scoreField, comparisonId, scoreLabel, algorithmNote) {
  const badge = comparisonBadge(status);
  const score = Number(scoreValue);
  const hasScore = hasSuppliedValue(scoreValue) && Number.isFinite(score);
  const track = hasScore
    ? `<div class="score-track kfs-score-track" data-kfs-score-track-field="${escapeHtml(scoreField)}" role="img" aria-label="Existing score ${escapeHtml(scoreValue)}"><span style="width:${Math.max(0, Math.min(100, score))}%"></span></div>`
    : `<div class="score-track kfs-score-track unavailable" data-kfs-score-track-field="${escapeHtml(scoreField)}" aria-label="Score not available"></div>`;
  const kicker = comparisonId === 'kfs-name' ? 'Name match' : 'Address match';
  return `<article class="comparison-card kfs-comparison-card" data-comparison="${escapeHtml(comparisonId)}"><div class="comparison-card-heading"><div><span class="card-kicker">${kicker}</span><h4>${escapeHtml(title)}</h4></div><span class="comparison-status status-${badge.css}" data-kfs-status-field="${escapeHtml(statusField)}">${escapeHtml(badge.label)}</span></div><div class="comparison-values"><div><span class="comparison-label">${escapeHtml(sourceLabel)}</span><p class="kfs-comparison-value" data-kfs-value-field="${escapeHtml(sourceField)}">${escapeHtml(userDetailValue(sourceValue))}</p></div><div><span class="comparison-label">${escapeHtml(kycLabel)}</span><p class="kfs-comparison-value" data-kfs-value-field="${escapeHtml(kycField)}">${escapeHtml(userDetailValue(kycValue))}</p></div></div><div class="comparison-score-line"><span>${escapeHtml(scoreLabel)}</span><b data-kfs-score-field="${escapeHtml(scoreField)}">${escapeHtml(hasSuppliedValue(scoreValue) ? scoreValue : 'Not available')}</b></div>${track}<p class="algorithm-note">${escapeHtml(algorithmNote)}</p></article>`;
}
function renderKfsDetailContent(r) {
  const compactCheck = value => ['Pass','Detected','Available','success'].includes(String(value)) ? 'Available' : 'Not available';
  const name = kfsComparisonCard('Borrower name vs KYC name','Borrower name',r.kfs_borrower_name,'kfs_borrower_name','KYC name',r.kyc_name,'kyc_name',r.kfs_borrower_name_match_status,'kfs_borrower_name_match_status',r.kfs_borrower_name_match_score,'kfs_borrower_name_match_score','kfs-name','Existing name score','Existing match status and score shown from stored values.');
  const address = kfsComparisonCard('Borrower address vs KYC permanent address','Borrower address',r.kfs_borrower_address,'kfs_borrower_address','KYC permanent address',r.kyc_permanent_address,'kyc_permanent_address',r.kfs_borrower_address_match_status,'kfs_borrower_address_match_status',r.kfs_borrower_address_match_score,'kfs_borrower_address_match_score','kfs-address','Existing address score','Existing match status and score shown from stored values.');
  const signatureNote = 'Structural presence only; not cryptographically verified.';
  return `<div class="form-card-grid kfs-form-card-grid"><section class="form-card kfs-borrower-details-card"><h4>KFS Borrower Details</h4><dl>${fieldCard('Borrower identification number',r.kfs_borrower_identification_number,'kfs_borrower_identification_number')}${fieldCard('Borrower name',r.kfs_borrower_name,'kfs_borrower_name')}${fieldCard('Borrower address',r.kfs_borrower_address,'kfs_borrower_address')}</dl><div class="kfs-comparison-list">${name}${address}</div></section><section class="form-card kfs-checks-card"><h4>KFS Checks</h4><dl>${fieldCard('Borrower Details match',compactCheck(r.kfs_borrower_details_match),'kfs_borrower_details_match')}${kfsEvidenceCard('Letterhead check (Logo)',r.kfs_letterhead_logo_check,'kfs_letterhead_logo_check','Text / structural evidence only; not visual logo recognition.')}${kfsEvidenceCard('Digital signature check',r.kfs_digital_signature_check,'kfs_digital_signature_check',signatureNote)}${fieldCard('Penal Charges check (page 4)',r.kfs_penal_charges_check,'kfs_penal_charges_check')}</dl></section></div>`;
}
function updateKfsFormFromRecord(r) {
  document.querySelectorAll('[data-kfs-value-field]').forEach(node => { node.textContent = userDetailValue(r[node.dataset.kfsValueField]); });
  document.querySelectorAll('[data-kfs-status-field]').forEach(node => {
    const badge = comparisonBadge(r[node.dataset.kfsStatusField]);
    node.textContent = badge.label;
    node.className = `comparison-status status-${badge.css}`;
  });
  document.querySelectorAll('[data-kfs-score-field]').forEach(node => {
    const value = r[node.dataset.kfsScoreField];
    node.textContent = hasSuppliedValue(value) ? String(value) : 'Not available';
  });
  document.querySelectorAll('[data-kfs-score-track-field]').forEach(track => {
    const value = r[track.dataset.kfsScoreTrackField], numeric = Number(value);
    const available = hasSuppliedValue(value) && Number.isFinite(numeric);
    track.classList.toggle('unavailable', !available);
    track.setAttribute('aria-label', available ? `Existing score ${value}` : 'Score not available');
    const fill = track.querySelector('span');
    if (fill) fill.style.width = available ? `${Math.max(0, Math.min(100, numeric))}%` : '';
  });
  document.querySelectorAll('[data-kfs-evidence]').forEach(card => {
    const key = card.dataset.kfsEvidence;
    card.classList.remove('kfs-evidence-positive', 'kfs-evidence-negative', 'kfs-evidence-neutral');
    card.classList.add(`kfs-evidence-${kfsEvidenceTone(r[key])}`);
  });
}
