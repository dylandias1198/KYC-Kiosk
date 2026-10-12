"""Regression coverage for the selected-record All Documents library."""
import pandas as pd

import app as app_module
from app import app


def _set_frame(frame):
    app_module.STATE.clear()
    app_module.STATE.update({"df": frame, "filename": "synthetic.xlsx", "path": None, "generation": 991})


def test_all_documents_tab_order_and_summary_remains_separate():
    _set_frame(pd.DataFrame([{"user_id": "u1"}]))
    with app.test_client() as client:
        html = client.get("/").get_data(as_text=True)
    tab_names = ["User Details", "KYC Details", "Form", "All Documents", "Summary"]
    positions = [html.index(f'>{name}</button>') for name in tab_names]
    assert positions == sorted(positions)
    assert "data-tab=\"all-documents\"" in html
    assert "data-tab=\"summary\"" in html
    assert "function renderAllDocumentsTab()" in html
    assert "function renderTab(tab)" in html
    assert "if (tab === 'all-documents') { renderAllDocumentsTab(); return; }" in html
    assert "renderAllDocuments()" in html


def test_initial_html_has_no_selected_record_document_urls_and_lazy_api_maps_sources():
    private_url = "https://documents.example.test/" + ("synthetic-private-object-" * 30) + ".pdf"
    frame = pd.DataFrame([{
        "user_id": "u1", "mitc_form_link": private_url,
        "application_form_link": private_url, "sanction_letter_link": private_url,
        "kfs_link": private_url, "gtc_link": private_url,
        "kyc_doc": "AADHAAR_OTP", "kyc_doc_link": private_url,
        "kyc_selfie_link": "https://images.example.test/kyc.png",
        "user_selfie": "https://images.example.test/user.png",
    }])
    _set_frame(frame)
    with app.test_client() as client:
        html_response = client.get("/")
        record_response = client.get("/api/records/0")
    html = html_response.get_data(as_text=True)
    assert html_response.status_code == record_response.status_code == 200
    assert private_url not in html
    assert "https://images.example.test/kyc.png" not in html
    assert "https://images.example.test/user.png" not in html
    assert "fetch(`/api/records/${index}`" in html
    record = record_response.get_json()["record"]
    for field in ("mitc_form_link", "application_form_link", "sanction_letter_link", "kfs_link", "gtc_link",
                  "kyc_doc", "kyc_doc_link", "kyc_selfie_link", "user_selfie"):
        assert record[field]
    for mapping in ("mitc_form_link", "application_form_link", "sanction_letter_link", "kfs_link",
                    "gtc_link", "kyc_doc_link", "kyc_selfie_link", "user_selfie"):
        assert f"source:'{mapping}'" in html
    assert "typeSource:'kyc_doc'" in html
    assert "data-all-doc-view" in html
    assert "safeHttpUrl(record[item.source])" in html


def test_all_documents_missing_links_and_statuses_are_truthful():
    record = {"user_id": "u2", "mitc_form_link": "", "application_form_link": "",
              "sanction_letter_link": None, "kfs_link": "not a URL", "gtc_link": "", "kyc_doc_link": "",
              "kyc_selfie_link": None, "user_selfie": "", "application_validation_status": "Not available"}
    _set_frame(pd.DataFrame([record]))
    with app.test_client() as client:
        response = client.get("/api/records/0")
    assert response.status_code == 200
    got = response.get_json()["record"]
    assert not got["mitc_form_link"] and not got["application_form_link"]
    assert got["sanction_letter_link"] == "" and got["kyc_doc_link"] == ""
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    assert "return 'Not supplied'" in template
    assert "return safeHttpUrl(value) ? 'Available' : 'Not available'" in template
    assert "return terminal" in template
    assert "Not supplied" in template and "Not available" in template
    assert "const status = allDocumentStatus(item, record);" in template
    assert "Summary" in template and "reviewSection('summary'" not in template


def test_all_documents_viewer_uses_existing_secure_document_routes_and_keeps_summary_clear():
    template = (app_module.BASE_DIR / "templates" / "index.html").read_text()
    for endpoint in ("mitc/document", "application/document", "sanction/document", "kfs/document", "gtc/document", "kyc/document"):
        assert endpoint in template
    assert "fetch(endpoint" in template
    assert "iframe class=\"all-document-frame\"" in template
    assert "target=\"_blank\" rel=\"noopener noreferrer\" referrerpolicy=\"no-referrer\"" in template
    assert "function viewAllDocument(key)" in template
    assert "function renderAllDocuments()" in template
    assert "reviewSection('user-details'" in template
    assert "reviewSection('kyc-details'" in template
    assert "reviewSection('form'" in template
    summary_branch = template.split("if (tab === 'all-documents')", 1)[1].split("let kycViewerRequestId", 1)[0]
    assert "reviewSection" not in summary_branch


def test_all_documents_renderer_truthfully_maps_available_and_missing_sources():
    """Execute the browser renderer in Node with an isolated selected record fixture."""
    from pathlib import Path
    import re
    import subprocess

    template = (Path(__file__).parents[1] / "templates" / "index.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    helpers = script[script.index("function hasSuppliedValue"):script.index("let photoZoomScale")]
    library = script[script.index("const allDocumentSources = ["):script.index("let kycViewerRequestId")]
    harness = r'''const userIds=['fixture']; let selectedIndex=0;
const record={user_id:'fixture',mitc_form_link:'https://fixture.test/mitc.pdf',application_form_link:'',sanction_letter_link:'https://fixture.test/sanction.pdf',kfs_link:'bad-url',gtc_link:'',kyc_doc:'AADHAAR_OTP',kyc_doc_link:'https://fixture.test/kyc.pdf',kyc_selfie_link:'https://fixture.test/kyc.png',user_selfie:''};
function selectedRecord(){return record;}
function escapeHtml(value){return String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');}
'''
    output = r'''const html=renderAllDocuments();
const result={cards:(html.match(/class="all-document-card"/g)||[]).length,
  missingApplication:allDocumentStatus(allDocumentSources.find(x=>x.key==='application'),record),
  invalidKfs:allDocumentStatus(allDocumentSources.find(x=>x.key==='kfs'),record),
  kycSource:allDocumentSources.find(x=>x.key==='kyc-document').source,
  kycType:allDocumentSources.find(x=>x.key==='kyc-document').typeSource,
  staleNoLinkWithSource:allDocumentStatus(allDocumentSources.find(x=>x.key==='mitc'),{...record,mitc_extraction_status:'no_link'}),
  kycCard:html.includes('data-source-column="kyc_doc_link"'),
  absentUserSelfie:html.includes('data-source-column="user_selfie"') && !html.includes('href=""')};
console.log(JSON.stringify(result));
if(result.cards!==8||result.missingApplication!=='Not supplied'||result.invalidKfs!=='Not available'||result.kycSource!=='kyc_doc_link'||result.kycType!=='kyc_doc'||result.staleNoLinkWithSource!=='Available'||!result.kycCard||!result.absentUserSelfie)process.exitCode=1;
'''
    completed = subprocess.run(["node", "-e", harness + helpers + library + output], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = __import__("json").loads(completed.stdout.strip())
    assert result["cards"] == 8
    assert result["missingApplication"] == "Not supplied"
    assert result["invalidKfs"] == "Not available"
    assert result["kycSource"] == "kyc_doc_link" and result["kycType"] == "kyc_doc"
    assert result["staleNoLinkWithSource"] == "Available"
