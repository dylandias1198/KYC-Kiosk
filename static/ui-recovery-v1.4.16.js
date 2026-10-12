/* v1.4.16: capture-phase UI recovery. It prevents duplicate handlers, invokes
   the normal renderer once, and only uses the compact fallback if the normal
   renderer leaves the loading placeholder visible. */
(function () {
  'use strict';
  var activeIndex = 0;
  function rows() { return Array.prototype.slice.call(document.querySelectorAll('.user-row')); }
  function escape(value) { var n = document.createElement('div'); n.textContent = value == null || value === '' ? 'Not supplied' : String(value); return n.innerHTML; }
  function getRecord(index) {
    return fetch('/api/records/' + index, {headers: {'Accept': 'application/json'}}).then(function (r) {
      return r.json().then(function (p) { if (!r.ok) throw new Error(p.error || 'Record could not be loaded'); return p.record || {}; });
    });
  }
  function isPlaceholder() {
    var el = document.getElementById('tabContent');
    return !el || /Select a user to view|Loading selected record/.test(el.textContent || '');
  }
  function fallback(index, tab, r) {
    var el = document.getElementById('tabContent'); if (!el) return;
    var name = r.user_name || r.user_id || 'Selected user';
    var pairs = tab === 'kyc-details' ? [
      ['KYC Name', r.kyc_name], ['KYC Father Name', r.kyc_father_name], ['KYC DOB', r.kyc_dob],
      ['KYC Gender', r.kyc_gender], ['KYC Type', r.kyc_status_type], ['KYC Mode', r.kyc_mode],
      ['KYC Status', r.kyc_status], ['Permanent Address', r.kyc_permanent_address], ['Current Address', r.kyc_current_address]
    ] : tab === 'form' ? [
      ['MITC', r.mitc_form_link], ['KFS', r.kfs_link], ['GTC', r.gtc_link],
      ['Sanction Letter', r.sanction_letter_link], ['Application Form', r.application_form_link]
    ] : [
      ['Name', r.user_name], ['Mobile', r.primary_mobile], ['Date of Birth', r.user_dob],
      ['Gender', r.user_gender], ['Permanent Address', r.user_perm_address], ['Current Address', r.user_curr_address]
    ];
    el.className = 'tab-content ' + (tab === 'kyc-details' ? 'kyc-details-view' : tab === 'form' ? 'form-layout' : 'user-details-view');
    el.innerHTML = '<div class="record-detail-view"><section class="selected-user-hero"><div class="hero-avatar">' + escape(String(name).slice(0,1).toUpperCase()) + '</div><div class="selected-hero-copy"><span class="hero-kicker">SELECTED RECORD</span><h2>' + escape(name) + '</h2><p>User ID · ' + escape(r.user_id) + '</p></div></section><section class="detail-section"><div class="detail-section-heading"><h3>' + escape(tab === 'kyc-details' ? 'KYC DETAILS' : tab === 'form' ? 'FORM' : 'USER DETAILS') + '</h3></div><dl class="fallback-detail-grid">' + pairs.map(function (p) { return '<div class="user-detail-card"><dt>' + escape(p[0]) + '</dt><dd>' + escape(p[1]) + '</dd></div>'; }).join('') + '</dl></section></div>';
  }
  function refresh(index, tab) {
    activeIndex = index;
    var row = rows().find(function (x) { return Number(x.dataset.index) === index; });
    rows().forEach(function (x) { x.classList.toggle('selected', x === row); });
    var heading = document.getElementById('selectedUser'); if (heading && row) heading.textContent = row.dataset.userId || '';
    try { if (typeof window.selectUser === 'function') window.selectUser(index); } catch (e) { console.error(e); }
    window.setTimeout(function () {
      if (index !== activeIndex || !isPlaceholder()) return;
      getRecord(index).then(function (r) { if (index === activeIndex && isPlaceholder()) fallback(index, tab, r); }).catch(function (e) { console.error(e); });
    }, 500);
  }
  function boot() {
    document.addEventListener('click', function (event) {
      var row = event.target.closest && event.target.closest('.user-row');
      if (row) { event.preventDefault(); event.stopImmediatePropagation(); refresh(Number(row.dataset.index), document.querySelector('.tab.active')?.dataset.tab || 'user-details'); return; }
      var tab = event.target.closest && event.target.closest('.tab');
      if (tab) { event.preventDefault(); event.stopImmediatePropagation(); document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('active', x === tab); x.setAttribute('aria-selected', x === tab ? 'true' : 'false'); }); try { window.renderTab(tab.dataset.tab); } catch (e) {} refresh(activeIndex, tab.dataset.tab); }
    }, true);
    var first = rows()[0]; if (first) refresh(Number(first.dataset.index), 'user-details');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true}); else boot();
}());
