(function () {
  'use strict';
  function rows() { return Array.prototype.slice.call(document.querySelectorAll('.user-row')); }
  function selectUser(row) {
    var index = Number(row && row.dataset ? row.dataset.index : NaN);
    if (!Number.isFinite(index)) return;
    rows().forEach(function (item) { item.classList.toggle('selected', item === row); });
    var heading = document.getElementById('selectedUser');
    if (heading) heading.textContent = row.dataset.userId || '';
    try { if (typeof window.kioskSelectUser === 'function') window.kioskSelectUser(index); else if (typeof window.selectUser === 'function') window.selectUser(index); }
    catch (error) { console.error('Kiosk user selection failed', error); }
  }
  function selectTab(tab) {
    var name = tab && tab.dataset ? tab.dataset.tab : '';
    if (!name) return;
    document.querySelectorAll('.tab').forEach(function (item) { var active = item === tab; item.classList.toggle('active', active); item.setAttribute('aria-selected', active ? 'true' : 'false'); });
    try { if (typeof window.kioskRenderTab === 'function') window.kioskRenderTab(name); else if (typeof window.renderTab === 'function') window.renderTab(name); }
    catch (error) { console.error('Kiosk tab render failed', error); }
  }
  function boot() {
    rows().forEach(function (row) { row.addEventListener('click', function (event) { event.preventDefault(); selectUser(row); }); });
    document.querySelectorAll('.tab').forEach(function (tab) { tab.addEventListener('click', function (event) { event.preventDefault(); selectTab(tab); }); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true}); else boot();
}());
