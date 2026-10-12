let viewerRequestId = 0;
let sourcePdfDocument = null;
let sourcePdfBlobUrl = '';
let sourcePdfPage = 1;
let sourcePdfScale = 1;
let sourcePdfObserver = null;
const PDFJS_URL = '/static/vendor/pdfjs/pdf.min.mjs';
function documentRoute() { if (selectedIndex < 0) return ''; const routes = {'MITC':'mitc/document','Sanction Letter':'sanction/document','Application Form':'application/document','KFS':'kfs/document','GTC':'gtc/document'}; return `/api/records/${selectedIndex}/${routes[currentFormType]}`; }
function pdfPageTarget(pageNumber) {
  const target = document.querySelector(`.pdf-page[data-page-number="${pageNumber}"]`);
  target?.scrollIntoView({behavior:'smooth', block:'start'});
  sourcePdfPage = pageNumber;
  const current = document.getElementById('pdfPageCurrent');
  if (current) current.textContent = String(pageNumber);
  document.querySelectorAll('.pdf-thumbnail').forEach(button => button.classList.toggle('active', Number(button.dataset.pageNumber) === pageNumber));
  updatePdfNavigation();
}
function updatePdfNavigation() {
  const previous = document.querySelector('[data-pdf-action="previous"]');
  const next = document.querySelector('[data-pdf-action="next"]');
  const zoomOut = document.querySelector('[data-pdf-action="zoom-out"]');
  const zoomIn = document.querySelector('[data-pdf-action="zoom-in"]');
  const total = sourcePdfDocument?.numPages || 0;
  if (previous) previous.disabled = !total || sourcePdfPage <= 1;
  if (next) next.disabled = !total || sourcePdfPage >= total;
  if (zoomOut) zoomOut.disabled = !total || sourcePdfScale <= .7;
  if (zoomIn) zoomIn.disabled = !total || sourcePdfScale >= 1.8;
}
async function renderPdfPage(pageNumber, canvas, maxWidth, scaleMultiplier = 1) {
  if (!sourcePdfDocument || !canvas || canvas.dataset.rendered === 'true') return;
  const documentRef = sourcePdfDocument;
  const page = await documentRef.getPage(pageNumber);
  if (documentRef !== sourcePdfDocument) return;
  const base = page.getViewport({scale:1});
  const scale = Math.min(maxWidth / base.width, 1.6) * scaleMultiplier;
  const viewport = page.getViewport({scale});
  const ratio = window.devicePixelRatio || 1;
  const context = canvas.getContext('2d', {alpha:false});
  canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
  canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;
  await page.render({canvasContext:context, viewport, transform:ratio !== 1 ? [ratio,0,0,ratio,0,0] : null}).promise;
  canvas.dataset.rendered = 'true';
}
function observePdfCanvas(pageNumber, canvas, maxWidth, multiplier = 1) {
  if (!('IntersectionObserver' in window)) { renderPdfPage(pageNumber, canvas, maxWidth, multiplier).catch(() => {}); return; }
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      observer.disconnect();
      renderPdfPage(pageNumber, canvas, maxWidth, multiplier).catch(error => {
        const box = canvas.closest('.pdf-page, .pdf-thumbnail');
        if (box) box.dataset.renderError = error.message || 'Unable to render this page';
      });
    }
  }, {root: canvas.closest('.pdf-thumbnails') || document.getElementById('pdfStage'), rootMargin:'180px'});
  observer.observe(canvas);
}
function buildPdfPages(pdf, initialPage) {
  const stage = document.getElementById('pdfStage');
  const thumbnails = document.getElementById('pdfThumbnails');
  if (!stage || !thumbnails) return;
  stage.replaceChildren(); thumbnails.replaceChildren();
  sourcePdfObserver?.disconnect(); sourcePdfObserver = null;
  const countLabel = document.getElementById('pdfPageTotal');
  if (countLabel) countLabel.textContent = String(pdf.numPages);
  const pageCheck = document.getElementById('pdfPageCheckStatus');
  if (pageCheck) pageCheck.querySelector('b').textContent = `PDF parsed · ${pdf.numPages} ${pdf.numPages === 1 ? 'page' : 'pages'}`;
  const openLink = document.getElementById('pdfOpenNewTab');
  if (openLink) { openLink.href = `${documentRoute()}#page=${initialPage}`; openLink.hidden = false; }
  for (let number=1; number<=pdf.numPages; number++) {
    const page = document.createElement('article');
    page.className = 'pdf-page'; page.dataset.pageNumber = String(number); page.setAttribute('aria-label', `PDF page ${number}`);
    const canvas = document.createElement('canvas'); canvas.className='pdf-page-canvas'; canvas.setAttribute('aria-label', `Page ${number} of ${pdf.numPages}`);
    page.append(canvas); stage.append(page);
    const thumb = document.createElement('button'); thumb.type='button'; thumb.className='pdf-thumbnail'; thumb.dataset.pageNumber=String(number); thumb.setAttribute('aria-label', `Go to page ${number}`);
    thumb.innerHTML = `<span class="pdf-thumbnail-canvas-wrap"><canvas aria-hidden="true"></canvas></span><span>Page ${number}</span>`;
    thumb.addEventListener('click', () => pdfPageTarget(number)); thumbnails.append(thumb);
    observePdfCanvas(number, canvas, Math.max(360, (stage.clientWidth || 700)-56), sourcePdfScale);
    observePdfCanvas(number, thumb.querySelector('canvas'), 90, .2);
  }
  sourcePdfPage = Math.max(1, Math.min(initialPage, pdf.numPages));
  requestAnimationFrame(() => pdfPageTarget(sourcePdfPage));
  if ('IntersectionObserver' in window) {
    sourcePdfObserver = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
      if (visible) {
        const number = Number(visible.target.dataset.pageNumber);
        sourcePdfPage = number;
        const current = document.getElementById('pdfPageCurrent'); if(current)current.textContent=String(number);
        document.querySelectorAll('.pdf-thumbnail').forEach(button => button.classList.toggle('active', Number(button.dataset.pageNumber)===number));
        updatePdfNavigation();
      }
    }, {root:stage, threshold:[.15,.4,.7]});
    stage.querySelectorAll('.pdf-page').forEach(page => sourcePdfObserver.observe(page));
  }
  updatePdfNavigation();
}
async function renderSourcePdf(bytes, initialPage, requestId) {
  const pdfjs = await import(PDFJS_URL);
  if (requestId !== viewerRequestId) return;
  pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
  const task = pdfjs.getDocument({data:new Uint8Array(bytes), cMapUrl:'/static/vendor/pdfjs/cmaps/', cMapPacked:true, standardFontDataUrl:'/static/vendor/pdfjs/standard_fonts/'});
  const pdf = await task.promise;
  if (requestId !== viewerRequestId) { await pdf.destroy(); return; }
  sourcePdfDocument = pdf;
  buildPdfPages(pdf, initialPage);
}
async function loadSourceDocument() {
  const state = document.getElementById('documentViewerState');
  const actions = document.getElementById('documentActions');
  const isSanction = currentFormType === 'Sanction Letter'; const isApplication = currentFormType === 'Application Form'; const isKfs = currentFormType === 'KFS'; const isGtc = currentFormType === 'GTC';
  const linkKey = isSanction ? 'sanction_letter_link' : isApplication ? 'application_form_link' : isKfs ? 'kfs_link' : isGtc ? 'gtc_link' : 'mitc_form_link';
  const link = selectedRecord()[linkKey] || '';
  if (!state) return;
  if (!link) { state.textContent = `No ${linkKey} supplied for this user.`; return; }
  const requestId = ++viewerRequestId;
  const page = isKfs ? 4 : 1;
  state.textContent = 'Loading source PDF…'; state.className = 'document-empty loading'; state.hidden = false;
  document.getElementById('pdfThumbnails')?.replaceChildren(); document.getElementById('pdfStage')?.replaceChildren(state);
  document.getElementById('pdfOpenNewTab')?.setAttribute('hidden','');
  document.getElementById('pdfPageCurrent').textContent = '—'; document.getElementById('pdfPageTotal').textContent = '—';
  actions.innerHTML = '';
  const badge = document.getElementById('documentStatus');
  if (badge && isGtc) { badge.textContent = 'Loading'; badge.className = 'status-badge'; }
  try {
    const response = await fetch(documentRoute(), {headers:{'Accept':'application/pdf'}});
    if (!response.ok) { let message=`Document unavailable (HTTP ${response.status})`; try { const payload=await response.json(); if(payload.error)message=payload.error; } catch (_) {} throw new Error(message); }
    const blob = await response.blob();
    if (requestId !== viewerRequestId) return;
    if (!blob.type.includes('pdf') && !(await blob.slice(0,5).text()).startsWith('%PDF-')) throw new Error('Source document is not a valid PDF');
    if (sourcePdfBlobUrl) URL.revokeObjectURL(sourcePdfBlobUrl);
    sourcePdfBlobUrl = URL.createObjectURL(blob);
    const pageCount = Number(response.headers.get('X-PDF-Page-Count') || 0);
    const actualPage = isKfs && pageCount > 0 && pageCount < 4 ? 1 : page;
    state.hidden = true;
    await renderSourcePdf(await blob.arrayBuffer(), actualPage, requestId);
    const note = document.getElementById('documentPageNote');
    if (note && isKfs && actualPage === 1) note.textContent = `KFS has ${pageCount || 'an unknown number of'} page(s); page 4 is unavailable, so viewer opens at page 1. You can still scroll through the entire PDF.`;
    if (badge && isGtc) { badge.textContent='Available'; badge.className='status-badge success'; const r=selectedRecord(); r.gtc_status='Available'; r.gtc_validation_error=''; updateFormFromRecord(); }
  } catch (error) {
    if (requestId !== viewerRequestId) return;
    state.hidden=false; state.className='document-empty document-error'; state.textContent=isGtc?'GTC document unavailable.':(error.message||'The source document could not be loaded.');
    if (badge && isGtc) { badge.textContent='Failed'; badge.className='status-badge failed'; const r=selectedRecord(); r.gtc_status='Failed'; r.gtc_validation_error=error.message||'GTC link unavailable'; updateFormFromRecord(); }
    const errorPanel=document.getElementById('documentError'); if(errorPanel && !isGtc) errorPanel.innerHTML=`<div class="form-error">${escapeHtml(error.message)}</div>`;
  }
}
function loadMitcDocument() { return loadSourceDocument(); }
document.addEventListener('click', event => {
  const control=event.target.closest('[data-pdf-action]');
  if (!control) return;
  const action=control.dataset.pdfAction;
  if(action==='previous')pdfPageTarget(Math.max(1,sourcePdfPage-1));
  if(action==='next')pdfPageTarget(Math.min(sourcePdfDocument?.numPages||sourcePdfPage,sourcePdfPage+1));
  if(action==='zoom-out'||action==='zoom-in'){
    const next=Math.max(.7,Math.min(1.8,sourcePdfScale+(action==='zoom-in'?.15:-.15)));
    if(next===sourcePdfScale)return; sourcePdfScale=next;
    document.querySelectorAll('.pdf-page-canvas').forEach(canvas=>{canvas.dataset.rendered='false';canvas.removeAttribute('width');canvas.removeAttribute('height');});
    document.querySelectorAll('.pdf-thumbnail canvas').forEach(canvas=>{canvas.dataset.rendered='false';canvas.removeAttribute('width');canvas.removeAttribute('height');});
    document.querySelectorAll('.pdf-page-canvas').forEach(canvas=>observePdfCanvas(Number(canvas.closest('.pdf-page').dataset.pageNumber),canvas,Math.max(360,(document.getElementById('pdfStage')?.clientWidth||700)-56),sourcePdfScale));
    document.querySelectorAll('.pdf-thumbnail canvas').forEach(canvas=>observePdfCanvas(Number(canvas.closest('.pdf-thumbnail').dataset.pageNumber),canvas,90,.2));
  }
});
