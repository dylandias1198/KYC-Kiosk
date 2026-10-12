"""Exercise the bundled PDF.js engine against generated local PDF bytes."""
from io import BytesIO
import gzip
from pathlib import Path
import shutil
import subprocess

from pypdf import PdfWriter


ROOT = Path(__file__).parents[1]

def test_bundled_pdfjs_loads_worker_and_parses_every_synthetic_page(tmp_path):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=420, height=600)
    writer.write(stream)
    pdf_path = tmp_path / "synthetic-local.pdf"
    pdf_path.write_bytes(stream.getvalue())
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the bundled PDF.js runtime"
    worker_path = ROOT / "static" / "vendor" / "pdfjs" / "pdf.worker.mjs"
    worker_path.write_bytes(gzip.decompress((ROOT / "static" / "vendor" / "pdfjs" / "pdf.worker.min.mjs.gz").read_bytes()))
    script = r"""
import fs from 'node:fs';
import * as pdfjs from './static/vendor/pdfjs/pdf.min.mjs';
pdfjs.GlobalWorkerOptions.workerSrc = new URL('./static/vendor/pdfjs/pdf.worker.mjs', import.meta.url).href;
const pdf = await pdfjs.getDocument({data:new Uint8Array(fs.readFileSync(process.argv[1]))}).promise;
if (pdf.numPages !== 2) throw new Error(`Expected 2 pages, got ${pdf.numPages}`);
const first = await pdf.getPage(1);
const second = await pdf.getPage(2);
const firstViewport = first.getViewport({scale:1});
const secondViewport = second.getViewport({scale:1});
if (firstViewport.width !== 612 || firstViewport.height !== 792) throw new Error(`Page 1 dimensions ${firstViewport.width}x${firstViewport.height}`);
if (secondViewport.width !== 420 || secondViewport.height !== 600) throw new Error(`Page 2 dimensions ${secondViewport.width}x${secondViewport.height}`);
console.log(`PDF.js parsed ${pdf.numPages} pages (${firstViewport.width}x${firstViewport.height}, ${secondViewport.width}x${secondViewport.height})`);
await pdf.destroy();
"""
    try:
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, str(pdf_path)],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
        )
    finally:
        worker_path.unlink(missing_ok=True)
    assert result.stdout.strip() == "PDF.js parsed 2 pages (612x792, 420x600)"


def test_gzip_worker_route_sets_browser_decompression_headers():
    from app import app
    with app.test_client() as client:
        response = client.get("/static/vendor/pdfjs/pdf.worker.min.mjs")
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.mimetype == "text/javascript"
    assert gzip.decompress(response.data).startswith(b"/**") or gzip.decompress(response.data).startswith(b"/*!\n * PDF.js")
