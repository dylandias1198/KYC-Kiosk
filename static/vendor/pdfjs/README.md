# Bundled PDF.js runtime

- Upstream package: `pdfjs-dist` version 4.10.38 (npm tarball used for this bundle).
- Runtime modules: `legacy/build/pdf.min.mjs` and the gzipped `legacy/build/pdf.worker.min.mjs`. Flask serves the worker from a local route with JavaScript MIME type and `Content-Encoding: gzip`, which lets the browser transparently decode the compressed worker.
- Runtime support files: upstream CMaps and standard fonts, copied unchanged.
- Main code is covered by `LICENSE` (Apache License 2.0); CMaps retain their included Adobe license notice and standard fonts retain their included Foxit/Liberation notices.
- These static assets run locally in the kiosk; PDF input continues through the app's existing selected-record document routes.
