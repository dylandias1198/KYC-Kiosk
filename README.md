# KYC Match Kiosk

A local Flask prototype for reviewing deterministic user-vs-KYC matching from an Excel workbook. It uses no external API integrations or credentials; supplied document URLs are fetched with ordinary verified HTTP(S) for local PDF viewing/extraction.

## Run locally

```bash
cd /workspace/data/kyc-match-kiosk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### macOS TLS setup

The MITC fetcher keeps TLS certificate and hostname verification enabled and uses the `certifi` CA bundle when available. On macOS, install the dependencies from the project virtual environment with `pip install -r requirements.txt`. If you are using the python.org distribution and its certificate bundle has not been installed, you may also run `/Applications/Python 3.x/Install Certificates.command` (use the versioned path present on your machine). The app does not disable verification or use an unverified SSL context.

Open <http://127.0.0.1:5000>, choose an `.xlsx` file, and select **Upload & Match**. The left list supports search and `↑`/`↓` keyboard navigation. The UI provides five tabs: User Details, KYC Details, Form, All Documents, and Summary. All Documents follows Form and precedes the separate Summary tab. After upload, **kyc_matching_output_v1.4.10.xlsx** is available via the download link exposed by the app route (`/download`; the matching-complete panel includes a visible **Download workbook** link).

## Workbook contract

The legacy required input columns are `user_id`, `um_uuid`, `lp_uuid`, `tenant`, `primary_mobile`, `user_name`, `user_dob`, `user_gender`, `user_perm_address`, `user_curr_address`, `mitc_type`, `mitc_date_latest`, `application_form_link`, `mitc_form_link`, `gtc_link`, `sanction_letter_link`, `kfs_link`, `kyc_status_type`, `products`, `kyc_mode`, `kyc_status`, `kyc_name`, `kyc_father_name`, `kyc_dob`, `kyc_gender`, `kyc_permanent_address`, and `kyc_current_address`. The expanded-format schema includes `user_selfie`, `kyc_selfie_link`, `kyc_doc`, `kyc_doc_link`, `other_metadata`, `verification_status`, `validation_type`, and `nsdl_date`; the complete expanded set is accepted, while legacy workbooks that contain none of these additions remain supported. `user_gender` remains beside user identity fields. Extra input columns and their source order are preserved in Enriched Data. The output workbook has `Enriched Data` and `Summary` sheets and never overwrites the uploaded source. Each sheet includes data rows (`Enriched Data` has one row per input; `Summary` has matching metric rows).

### User Details tab source mapping
The selected User Details screen lazily retrieves the selected row from `GET /api/records/<index>` and presents a dark teal selected-user hero (full name, user ID, initials, source-backed chips, keyboard-navigation hint, and user position). The page is divided into **PERSONAL INFORMATION**, **ADDRESS INFORMATION**, and **APPLICATION METADATA**. Personal cards use only `Name` ← `user_name`, `Mobile` ← `primary_mobile`, and `Date of Birth` ← `user_dob`; the photo uses only `user_selfie`. Address cards map `Permanent Address` ← `user_perm_address` and `Current Address` ← `user_curr_address`; a `Match` badge appears only when the corresponding existing `permanent_address_match_status` or `current_address_match_status` is exactly `Match`. Metadata maps `MITC Type` ← `mitc_type`, `MITC Date` ← `mitc_date_latest`, `NSDL Pull Date` ← `nsdl_date`, and `APV Status` ← `verification_status`. Missing values display `Not supplied`, and status chips are omitted when their source is missing. User photo URLs must be HTTP(S), are escaped, have accessible alt text and an `Open image` link, and show an error placeholder on browser load failure; photos are not downloaded, stored, or logged. Initial HTML contains no selected-record values or photo/document URLs.

Both the User Details **User Photo** card and each available selfie card in KYC Details provide a **Zoom photo** button. The local in-browser lightbox supports zoom in/out, reset-to-fit, closing with its labeled close button, clicking the backdrop, or pressing Escape; keyboard focus moves into the dialog and remains navigable among controls. Mouse-wheel zoom is enabled only while the lightbox is open and the pointer is over it. Invalid/missing photo URLs and browser image-load failures do not offer a zoom button. Photos remain ordinary HTTP(S)-only browser-loaded resources with `referrerpolicy="no-referrer"`; the lightbox does not download or store image files. Opening a preview may cause the browser to load the source URL again (or use its cache); the app does not guarantee remote-host privacy beyond the no-referrer policy and does not control that host's access logs.

The repository includes `sample_input.xlsx`, a 15-row fixture using the supplied user IDs, names, user/KYC gender pairs, `LAZYPAY` tenant, and `["PAYUFIN_KYC"]` product value. The task payload did not provide the remaining record-level DOB, address, status, mode, or document-link values; those fields are present as blank cells rather than invented. Run `python make_sample.py` to regenerate it.

## Matching algorithms

* **Text normalization:** Unicode NFKD transliteration to ASCII, case folding, punctuation removal, and whitespace collapse. Names additionally remove only recognized leading honorifics (`Mr`, `Mrs`, `Ms`, `Miss`, `Dr`, `Prof`, `Shri`, `Smt`, `Mdm`).
* **Name:** calculate RapidFuzz `token_set_ratio`, `token_sort_ratio`, and `ratio`; the maximum is the score. A score of at least **60** is `Match` (the named `NAME_MATCH_THRESHOLD` constant).
* **DOB:** parse Excel serials/datetimes, `DD-MMM-YYYY`, `DD-MM-YYYY`, `DD/MM/YYYY`, ISO dates/datetimes, common dotted dates, and weekday/timezone strings such as `Tue Nov 01 18:30:00 GMT 2005`. Weekday/timezone strings preserve the displayed calendar date without timezone shifting; all DOB comparisons remain exact day/month/year. Compare exact day/month/year. Invalid and missing values have explicit statuses.
* **Address:** expand common abbreviations, calculate the same three token/string similarities, and incorporate house-number and six-digit PIN signals. A different present PIN caps the score below the match threshold; a score of at least **60** is `Match` (the named `ADDRESS_MATCH_THRESHOLD` constant).
* **Gender:** normalize with Unicode/punctuation/whitespace cleanup and case folding. `F`, `female`, `fem`, `woman`, and `women` map to `FEMALE`; `M`, `male`, `man`, and `men` map to `MALE`; `O`, `other`, `others`, `non-binary`, `nonbinary`, `third gender`, `transgender`, and `T` map to `OTHER`. Any other non-empty value is retained as its own uppercase canonical category. Canonical values compare exactly. The output preserves `user_gender_normalized` and `kyc_gender_normalized`, plus `gender_match` and `gender_match_score` (`100`/`0` for present matches/mismatches and blank for missing inputs).

Statuses include `Match`, `Not a Match`, `Review` (overall), `Both not present`, `User/KYC name not present`, `User/KYC dob not present`, `User/KYC gender not present`, `User/KYC permanent/current address not present`, and `Invalid user/KYC dob`. Gender status is included in the overall result and in Summary counts.


### MITC timeout and retry settings

The defaults are intentionally bounded and can be overridden before starting the app with environment variables:

* `MITC_FETCH_TIMEOUT_SECONDS` — positive per-attempt urllib timeout in seconds; default `60` (not 15). The timeout applies to each connection/read attempt, including the response read.
* `MITC_FETCH_RETRIES` — non-negative number of retries after the initial request; default `2` (maximum of three total attempts).

For example:

```bash
MITC_FETCH_TIMEOUT_SECONDS=90 MITC_FETCH_RETRIES=3 .venv/bin/python app.py
```

Retries apply only to socket/read timeouts, connection resets, and transient HTTP 5xx fetch failures. Blank or invalid URLs, HTTP 4xx responses such as 404, non-PDF bytes, oversized responses, and malformed PDFs fail immediately without retry. The final error includes the last attempt number, total attempts, timeout, and retry setting. HTTPS continues to use verified TLS with certifi when available; SSL verification is never disabled. The retry backoff is short and capped, so total request work remains bounded.

## Tests

```bash
pytest -q
python smoke_test.py
```

The smoke test uses Flask's local test client to upload the fixture, checks that 15 IDs are returned and all supplied gender pairs are `Match`, verifies arrow-navigation/tab controls are present in the rendered UI, and validates both workbook sheets and enrichment columns. It does not start a public server or call an external service.

## MITC form extraction (LP profile active)

The workspace tabs are **User Details**, **KYC Details**, **Form**, **All Documents**, and **Summary**. The **Form** screen remains dedicated to form checks. Its dropdown supports exactly `MITC`, `KFS`, `Sanction Letter`, `Application Form`, and `GTC`. MITC, Sanction Letter, Application Form, KFS, and GTC have functional document viewing; Application Form and KFS run the checks documented below, while GTC reports link availability without extracting fields.

LP MITC extraction uses that row's `mitc_form_link`; Sanction Letter extraction and viewing use that row's `sanction_letter_link` (never `mitc_form_link`). LP extraction and the preserved other-document checks run on the bounded background worker pool after upload, so the initial workbook response does not wait for downloads. Selected-record endpoints can queue/reuse work. A process-local PDF-byte cache keyed by exact URL is shared by extraction and all viewer routes, and parsed result caches are URL-keyed as well; repeated or cross-document reads of the same URL do not redownload it. Failed fetches are not cached, allowing retry. HTTPS verification remains enabled with certifi when available.

In the current implementation, the active MITC profile is **LP only**. A visible profile selector offers exactly `LP`, `FPL`, and `MPL`. LP extracts its three values into LP-specific fields from the selected row's `mitc_form_link`; FPL and MPL explicitly report **not implemented yet** and do not modify or relabel the LP result. The prior FPL borrower/KYC fields are not applied to LP. The active LP flow runs only the LP parser: the three extracted content values are stored in `lp_mitc_*` fields, and legacy generic borrower/KYC/contact/address columns remain blank or neutral/not applicable for LP. Those compatibility columns are not populated by an FPL-style parser or presented as LP results.

LP label extraction targets page 1 and scans only the bounded first 12 pages as needed, stopping once all three fields have been found; LP never loads or parses page 23. The active LP viewer opens at page 1. It extracts without example-value constants: `lp_mitc_date` from `Date` (DD-MM-YYYY, DD/MM/YYYY, and ISO date forms), `lp_mitc_customer_identification_number` from `Customer identification number`, and `lp_mitc_borrower_name` from `Name of the Borrower`. `lp_mitc_borrower_name` is compared only with that row's `kyc_name` using the existing `normalize_name`/`match_name` logic and `NAME_MATCH_THRESHOLD=60`; there is no `user_name` fallback. Status/error columns distinguish successful, partial, failed, and absent-link extraction.

LP signature checking inspects the PDF catalog/AcroForm for `/FT /Sig` signature fields and signature dictionaries containing `/ByteRange` and `/Contents`. The compact UI reports only `Detected` or `Not Detected`, and displays signature page metadata when recorded; internal workbook/API evidence identifies the structural elements and explicitly says structural presence is not cryptographic verification. No finding is reported as `Not Detected`; visible `Digitally signed by ...` text and visible images are explicitly not signature evidence. The app does not validate certificate chains, byte-range integrity, revocation, or cryptographic signature validity, and never returns Pass based on a field's presence. Other document checks retain their existing behavior. FPL's prior format and MPL's future format are intentionally out of scope in this LP-only iteration; they will need their own separately implemented profiles before activation.

Sanction Letter parsing uses the bounded configurable fallback scan described under v1.3.0 for sanction date, reference number, borrower name, Annexure and the phrase `Terms and conditions of the sanction`; it also inspects the last page separately for signature evidence and first three pages for branding. `sanction_borrower_details_match` mirrors the name status, not an independent or invented boolean. Annexure/terms is Pass only when both the Annexure and required terms phrase occur in extracted text. The sanction viewer opens at page 1; active LP MITC opens at page 1 (generic/FPL legacy remains page 23). Both viewers remain scrollable.

The `Enriched Data` sheet retains all source and matching columns and every existing generic MITC compatibility column, then adds these exact LP/profile fields and Application/KFS/GTC fields documented below. LP validation status/error remain in workbook/API for processing while omitted from LP Form screen; Sanction-only hidden fields and source links/errors for GTC likewise remain internal. `document_checks.py` contains the preserved Application Form, KFS, and GTC parsing/check workflows:

* `mitc_profile`
* `lp_mitc_date`
* `lp_mitc_customer_identification_number`
* `lp_mitc_borrower_name`
* `lp_mitc_borrower_name_match_status`
* `lp_mitc_borrower_name_match_score`
* `lp_mitc_validation_status`
* `lp_mitc_validation_error`
* `lp_mitc_digital_signature_check`
* `lp_mitc_digital_signature_evidence`
* `lp_mitc_digital_signature_page`

The three LP-extracted PDF content fields are exactly `lp_mitc_date`, `lp_mitc_customer_identification_number`, and `lp_mitc_borrower_name`. Remaining LP-prefixed outputs are match/validation/signature metadata.

The pre-existing generic MITC columns (including `mitc_form_date`, borrower/KYC detail, generic name match, logo/signature evidence, extraction and validation state) stay present for workbook compatibility. For LP, the generic borrower/KYC/contact/address fields remain blank or neutral/not applicable. The LP Form view displays only `Date`, `Customer identification number`, and `Name of the Borrower / Borrower Name` as extracted document fields, plus KYC Name and the LP borrower-name-vs-KYC match status/score. Internal LP validation status/error remain in workbook/API only. Compact checks show `Page check` / `Signature page`, `Digital signature check` (Detected/Not Detected based only on structural `/FT /Sig` or `/ByteRange` + `/Contents` presence, never cryptographic validity), and `Letterhead check (Logo)` (Detected only on early-page PayU Finance branding text; embedded image presence is not claimed to be visual logo recognition). LP Form viewer and parser target page 1 / early pages only; they never parse page 23. Generic/FPL legacy MITC viewer behavior retains page 23 where applicable.
The output also adds these exact Sanction Letter columns:

* `sanction_letter_date`
* `sanction_letter_reference_number`
* `sanction_borrower_name`
* `sanction_borrower_name_match_status`
* `sanction_borrower_name_match_score`
* `sanction_borrower_details_match`
* `sanction_letterhead_logo_check`
* `sanction_letterhead_logo_evidence`
* `sanction_digital_signature_check`
* `sanction_digital_signature_evidence`
* `sanction_annexure_terms_check`
* `sanction_annexure_terms_evidence`
* `sanction_validation_status`
* `sanction_validation_error`

MITC status fields are `mitc_extraction_status` / `mitc_extraction_error`; LP-specific validation is in `lp_mitc_validation_status` / `lp_mitc_validation_error`; sanction state is in `sanction_validation_status` / `sanction_validation_error`. Rows expose `queued`, `extracting`, `success`, `partial` (MITC uses the legacy `parsed_with_missing_fields`), `failed`, and `no_link` states as applicable. Selected-record JSON and the workbook retain these statuses. The dashboard preserves its MITC progress summary and adds sanction eligible/completed/queued-or-extracting/failed counts.

Limitations: text extraction does not OCR scanned/image-only PDFs, so fields or checks may be `Review` / `Not available` or partial when no text layer exists. Embedded image presence is not definitive logo or signature classification. Logo text matching demonstrates branding text only, not a visual logo. The parser intentionally bounds pages scanned; documents with materially different labels or fields beyond the scan bound can remain partial. A reference number absent from the PDF text is left blank. The attached local example may be inspected for development but is not a committed test fixture; automated tests use synthetic/mock PDF text and never contact live S3.

Run with `python app.py`. Tests: `pytest -q` and `python smoke_test.py`; smoke tests use the local sample workbook and Flask's local test client, no external services.

## MITC document viewer

The **Form** tab includes a scrollable **MITC Document** panel beside the extracted MITC cards on wider screens and above/below them on smaller screens. It follows the currently selected left-side user; changing the user invalidates the previous viewer request and loads the new row's `mitc_form_link`. After extraction, the viewer remains available independently of the extracted fields.

The local route `GET /api/records/<record_index>/mitc/document` performs the same credential-free, bounded HTTP fetch used by extraction. Its URL-keyed in-memory byte cache is shared with Sanction Letter, Application Form, KFS, and GTC viewer/extractor routes. Document routes are `GET /api/records/<record_index>/<mitc|sanction|application|kfs|gtc>/document` (MITC keeps the legacy path). It validates the URL, requires a PDF signature, limits the response to 20 MB, and validates the bytes with `pypdf` before returning `Content-Type: application/pdf` and `Content-Disposition: inline`. The browser embeds the response in a fixed-height, vertically scrollable iframe and offers **Open in new tab**. The legacy frontend constant `MITC_PREVIEW_PAGE = 23` remains for the generic/FPL legacy path; active LP MITC overrides it and opens at page 1. LP MITC parsing and viewer do not access page 23. The Form panel shows the active LP target in a visible note. Missing links, failed/unreachable fetches, non-PDF bytes, oversized documents, and malformed PDFs return truthful JSON HTTP errors; the Form panel displays the error or a no-document state instead of fabricating content or crashing.

Viewer limitations: the browser must be able to reach the local kiosk route and the source PDF must be available to the kiosk's network; only PDF documents are supported; scanned/image-only PDFs can still be displayed, but text extraction may remain blank because MITC extraction is label/text based and does not perform OCR. Tests use mocked fetches and a synthetic local PDF; they do not call external S3.

## MITC background extraction architecture

MITC extraction is a bounded, process-local background batch. On upload, the original generic MITC columns and LP-specific output columns are initialized for every row. Rows without a usable `mitc_form_link` are marked `no_link` with the explicit `No mitc_form_link supplied` error and never make a network call; valid links begin in `queued` and move to `extracting`, `success`, `parsed_with_missing_fields`, or `failed`. A `ThreadPoolExecutor` with **4 workers** processes the batch without delaying the upload response.

The in-process URL cache is protected by a lock. A second row with the same URL shares the existing in-flight Future and receives the same result, so duplicate downloads and parses are avoided. The selected-row `POST /api/records/<index>/mitc` remains compatible: it returns the current row immediately and submits the URL only if needed. `GET /api/records/status` returns `records`, per-row `index`/`extraction_status`, `pending`, and the live `summary` metrics documented below; the Form view polls it while any row is queued or extracting and updates the header/card fields without re-fetching the PDF viewer. The MITC viewer remains independent and the active LP version opens at page 1; the legacy generic/FPL path retains the page-23 default. Sanction Letter viewing starts at page 1.

`MITC_TARGET_PAGE = 23` remains a 1-based constant for generic/FPL legacy parsing. LP extraction does not use that target: it scans only page 1 through the first 12 early pages, stopping when its three labels are satisfied, and never accesses page 23. LP uses only its LP-specific parser; generic FPL-era MITC borrower/KYC/contact/address fields remain blank/not applicable for compatibility and are not separately populated from the same PDF. Image-only pages and scanned PDFs remain limitations.

Background completion writes are debounced/coalesced through a short quiet period (0.4 seconds by default) and serialized under the workbook write lock. A fresh snapshot is atomically replaced from a temporary `.xlsx` using `os.replace`, preserving all rows and all original and added MITC columns. `/download` cancels and flushes the pending write synchronously; upload serializes its replacement against background writers. Failed results are not cached, so a selected-row `POST /api/records/<record_index>/mitc` can retry after a timeout without restarting the process; a later upload also starts a fresh attempt. Successful and `parsed_with_missing_fields` URL results remain cached for the process lifetime. The worker callback always converts unexpected worker exceptions into a terminal `failed` row, so no row remains `extracting` forever. Tests use synthetic or mocked PDFs and never call live S3.

## MITC extraction progress summary

After upload, the matching-complete header card shows a live **MITC extraction** summary. The denominator is explicitly the number of records with a nonblank `mitc_form_link` (`eligible`), not the total workbook size. The displayed extracted numerator is only `success` plus `parsed_with_missing_fields`; queued, extracting, failed, and no-link rows are never presented as extracted. The card includes total records, eligible links, success, parsed-with-missing-fields, failed, no-link, queued, and in-progress counts, plus a compact percentage/progress bar. Thus an upload can truthfully start at `MITC extracted 0 / N eligible` while background work is queued or extracting.

`GET /api/records/status` remains backward-compatible with its existing `records`, `rows`, and `pending` keys and now also returns `summary` with `total_records`, `eligible`/`eligible_mitc_links`, `extracted`, `success`, `parsed_with_missing_fields`, `failed`, `no_link`, `queued`, `extracting`, and `progress_percent`/`progress_percentage`. The browser polls the lightweight `/api/records/status/summary` route every 700 ms while work is pending and applies summary metrics plus row status changes without re-rendering the full user list. A selected-row status change refreshes only that selected record. The browser never calls full `/api/records/status` on initial load or during polling. With zero eligible links, the percentage is `0.0` and the denominator remains zero rather than being inferred from row count.


## Large-workbook loading

The initial kiosk response contains only the lightweight user-list fields (`index`, `user_id`, `user_name`, `mitc_extraction_status`, and `overall_match_status`) plus progress summary data; it does not serialize the enriched record set or long S3 document URLs. The fixed-height left list supports search by ID/name and arrow-key navigation. Full records are fetched from `GET /api/records/<record_index>` only as needed for selection/Form rendering and cached in the browser for the session. `/api/records` remains available for compatibility.

The browser polls `GET /api/records/status/summary?profile=<LP|FPL|MPL>` every 700 ms while MITC, Sanction Letter, Application Form, KFS, or GTC work is pending. Profile is also supplied to MITC extraction and status requests; selecting FPL or MPL returns a profile-specific not-implemented state and does not clear or reinterpret the LP result. The compact payload includes per-row document status tuples, not full documents/records, so selection-specific details are refreshed only when a status changes. That payload contains MITC summary metrics, separate sanction progress counts, and per-row MITC/sanction statuses only; when the selected row changes status, the browser refreshes only that record. Callers that need the legacy full status payload can keep using `/api/records/status` (or request compact compatibility mode with `?full=0`). Background completions coalesce through a short quiet-period debounce and the existing workbook write lock/atomic replace. `/download` cancels a pending timer and synchronously flushes a fresh snapshot, retaining the latest workbook state without allowing an older queued snapshot to overwrite it.


## Application Form, KFS, and GTC checks

The versioned output is `outputs/kyc_matching_output_v1.2.7.xlsx`; the Application Form, KFS, and GTC extraction/check implementation is included in `document_checks.py`; the workbook keeps the original input columns, existing match/MITC/Sanction columns, plus the exact added columns below. Every row receives these columns even when its source link is blank.

**Application Form** uses only the row's `application_form_link` and parses page 1 only. Deterministic label boundaries support inline/newline labels and line-wrapped labels, with repeated City/Pincode/State/Country values assigned by their Communication Address or Current Address section. Values stop at the next recognized label; punctuation is tolerated and literal `N/A`, `-`, and masked PAN values are retained. Extracted fields are `application_borrower_name` (Name of the Borrower), `application_father_name`, `application_mother_name`, `application_nationality`, `application_dob`, `application_gender`, `application_mobile_number`, `application_email`, `application_pan`, `application_other`, `application_communication_address`, `application_communication_city`, `application_communication_pincode`, `application_communication_state`, `application_communication_country`, `application_current_address`, `application_current_city`, `application_current_pincode`, `application_current_state`, `application_current_country`, and `application_occupation_type`. No example values are embedded in the parser; absent values remain blank and validation records missing labels. Address components use City, Pincode/Pin Code, State, and Country labels within each respective address group. Page 1 text or existing embedded-image evidence is retained in internal `application_letterhead_logo_evidence`; the compact `application_letterhead_logo_check` displays only `Available` or `Not available`. Available means page-one PayU Finance branding text or an embedded image resource was found, not that a logo was visually classified. The UI does not display verbose evidence.

Only three requested Application Form comparisons are run: borrower name vs `kyc_name` using existing normalization and the 60-point name threshold; DOB vs `kyc_dob` using supported date normalization and exact calendar-date equality; gender vs `kyc_gender` using existing normalization. `application_borrower_details_match` is derived solely from those three statuses (Match when all match, Not a Match on an explicit mismatch, otherwise Review). No address/contact/parent comparison is inferred. `application_photo_match` is always `Not implemented`: an image's presence is not a face/photo match. The Application Form UI shows a separate safely escaped card for every extracted field in a full-width extracted-fields panel beside the source PDF, the three comparisons, and only compact `Letterhead check (Logo): Available/Not available` and `Photo match: Not implemented` evidence/limitations. Internal evidence and errors remain in workbook/API. Application Form viewer starts at page 1 and remains scrollable.

Application workbook columns include all 21 extracted fields listed above, followed by `application_name_match_status`, `application_name_match_score`, `application_dob_match_status`, `application_dob_match_score`, `application_gender_match`, `application_gender_match_score`, `application_borrower_details_match`, `application_letterhead_logo_check`, `application_letterhead_logo_evidence`, `application_photo_match`, `application_validation_status`, and `application_validation_error`.

**KFS** uses only `kfs_link`; LP borrower identification number, name, and address are parsed from page 3 only, while the Penal Charges check examines page 4 only (1-based). It returns Pass only for text matching `Penal Charges, if any, in case of delayed payment`, `Penal charges/Late Payment Charges, if any, in case of delayed payment`, or `Other penal charges if any` (whitespace/case variants tolerated). It does not assert a pass from screenshots or other pages. No extractable text or a PDF shorter than four pages yields `Review` with a specific note/error; fetch or parse failures yield `Failed`. KFS borrower name/address comparisons use the existing 60-point thresholds against `kyc_name` and `kyc_permanent_address`. The KFS letterhead check uses page-1/page-3 branding text only, never embedded-image presence as a logo claim. Structural signature and branding evidence are compact in the UI but retained internally. Workbook/API fields include `kfs_borrower_identification_number`, `kfs_borrower_name`, `kfs_borrower_address`, the two match status/score pairs, `kfs_borrower_details_match`, letterhead/signature check and evidence, `kfs_penal_charges_check`, `kfs_penal_charges_evidence`, `kfs_validation_status`, and `kfs_validation_error`. The viewer opens at page 4 when present, otherwise page 1 with a truthful short-PDF note; it is scrollable.

**GTC** uses only `gtc_link` and extracts no document fields. Blank links are `Not available` with `No gtc_link supplied`; nonblank links are checked asynchronously through the shared verified-TLS PDF fetch/cache and appear `Loading` while checked, then `Available` only after successful fetch. A failed attempted fetch becomes `Failed` with the error in `gtc_validation_error`. The Form viewer route can open the PDF at page 1; a viewer failure updates the visible status/error. Columns: `gtc_status`, `gtc_validation_error`.

Status defaults are explicit: absent Application/KFS links have `Not available`; malformed links and attempted worker/fetch errors are `Failed`; successfully fetched but partially extractable Application Forms remain `partial` and describe missing fields. Application, KFS, and GTC source URLs are not presented as document-derived facts. The Form dropdown remains exactly MITC, KFS, Sanction Letter, Application Form, GTC. Viewer start pages are active LP MITC 1, legacy generic/FPL MITC 23, KFS 4 (fallback 1 for shorter PDFs), and Application Form/GTC/Sanction Letter 1.

**Speed and isolation:** after upload, nonblank, valid Application Form and KFS URLs are queued on the existing bounded worker pool, and GTC links are checked there as well. URLs are deduplicated per document type, shared URL PDF bytes use one lock-protected cache so same-URL document types do not redownload, and only the compact status-summary route is polled for live updates. The initial page remains lazy: it returns user-list fields rather than full records or document links; selected records load on demand. Background results are coalesced into atomic versioned workbook writes; a failed row does not block other rows and failures remain retryable. TLS verification, timeout/retry policy, and 20 MB PDF bounds are unchanged. Automated tests use synthetic/mock PDF content and bytes only; no live S3 fetch is required.

### KYC Details tab source and comparison mapping
The **KYC Details** view loads only the selected row through `GET /api/records/<index>`. Its selected-user hero shows full name, user ID, and chips based on `kyc_status_type`, `kyc_mode`, and `kyc_status`; the `ALL MATCHES` / `REVIEW` indicator is based on the five existing statuses only. The two-column screen has **KYC SOURCE DATA** and **COMPARISON RESULTS**. Source cards map `User Selfie` ← `user_selfie`, `KYC Selfie` ← `kyc_selfie_link`, `KYC Type` ← `kyc_status_type`, `KYC Mode` ← `kyc_mode`, `KYC Status` ← `kyc_status`, `KYC Name` ← `kyc_name`, `KYC Father Name` ← `kyc_father_name`, `KYC Date of Birth` ← `kyc_dob`, `KYC Gender` ← `kyc_gender`, `KYC Permanent Address` ← `kyc_permanent_address`, and `KYC Current Address` ← `kyc_current_address`. These are kept separate from user-source values; FPL-only MITC fields are not shown in KYC Details.

The comparison score cards display the existing `name_match_score`, `dob_match_score`, `gender_match_score`, `permanent_address_match_score`, and `current_address_match_score` values without re-running or recalculating the matching algorithms. Passed checks count only statuses whose value is exactly `Match`; absent values and other status text do not count. The five comparison cards show User Value, KYC Value, existing status and score, score bar (visual scale only), and an algorithm note tied to the implemented matching logic. Gender displays `user_gender_normalized` / `kyc_gender_normalized` when supplied, otherwise the raw `user_gender` / `kyc_gender`; no other value is substituted. Missing source fields display `Not supplied`, and missing scores display `Not available`. The selfie links use the same safe HTTP(S)-only, escaped, `no-referrer`, accessible image/link and error-state behavior as User Details; no media is fetched into the UI except through normal browser image loading, and no selfie is downloaded, stored, or logged. Initial HTML and compact status polling remain free of KYC values and URLs.


## v1.2.6 display and page-target refinements

Versioned output for this release is `outputs/kyc_matching_output_v1.2.7.xlsx`. Both User Details and KYC Details selfie cards use a flex-column control layout with a clear gap between **Zoom photo** and **Open image**; the accessible zoom modal and keyboard behavior are unchanged.

Sanction Letter Form displays only sanction date, borrower name, and borrower-name match against KYC. Its reference number, separate KYC name, details-match field, and letterhead/signature/annexure evidence checks remain internal only. GTC Form displays only **GTC availability** and **Link status**, with Available/Not available by default and Failed only after an attempted fetch fails; source URL and explanatory errors remain internal.

KFS Form displays the page-3 borrower ID/name/address and name/address match status/score, compact Borrower Details/letterhead/signature statuses, and the existing page-4 Penal Charges check. Page 3 and page 4 are read through the shared URL-keyed PDF-byte cache with targeted page text extraction (not a full text scan); structural signature evidence remains in the workbook/API, not expanded in the compact UI.

## v1.2.7 Application Form extraction

Versioned output is `outputs/kyc_matching_output_v1.2.7.xlsx`. Application Form page-one parsing now binds each value to the next recognized field label and separates repeated address components by the Communication Address / Current Address section. The compact Form evidence labels are exactly **Letterhead check (Logo): Available/Not available** and **Photo match: Not implemented**. Branding evidence is preserved internally; an embedded image resource is only evidence of an image, not proof that a logo was visually recognized. Photo availability likewise is not a photo match. The other v1.2.6 UI/page-target behaviors remain in place.

Run `pytest -q`, `python smoke_test.py`, `node --check` on the embedded script (see the validation command in the task log), and inspect `outputs/kyc_matching_output_v1.2.7.xlsx` for `Enriched Data` and `Summary`.

## v1.2.8 KYC document source viewer

Versioned output is `outputs/kyc_matching_output_v1.2.8.xlsx`. The KYC Details **KYC Document** card maps **KYC Document Type** exactly to the input `kyc_doc` field and **KYC Document Link** exactly to `kyc_doc_link`; blank values render as **Not supplied**. KYC selfie fields (`kyc_selfie_link` / `kyc_selfie_doc`) are independent and are never used as a document link. Original source columns remain in Enriched Data. Additional workbook columns are `kyc_document_status`, `kyc_document_error`, and `kyc_document_source_type`; unsupported types, absent direct links, and missing conversion metadata begin as **Not available**, while available sources remain **Not requested** until the viewer is opened. Workbook source XML/base64 is retained as the original column name, but if it contains XML or base64-encoded content its cell is replaced by a redaction marker in generated workbooks. The in-memory source record is never returned to the browser with `other_metadata`.

The selected-row route is `GET /api/records/<index>/kyc/document`; it reads that row's selected `kyc_doc`, `kyc_doc_link`, and (only for conversion types) `other_metadata`. No document is downloaded during upload. Initial HTML and lightweight polling omit all source document URLs. The viewer fetch is user initiated, scoped to the currently selected row, starts PDF view at page 1, offers **Open in new tab**, and renders validated PDF or image content in a scrollable inline viewer. Response types are limited to `application/pdf`, `image/jpeg`, `image/png`, and `image/webp`, with PDF parsing, Pillow image decoding/verification, dimension bounds, signature/truncation checks, and a 20 MB limit. Fetching uses verified TLS with certifi when available, bounded 15-second requests and up to two transient retries, and the shared process-local PDF byte cache; image/conversion caches are process-local. Errors do not echo source URLs or document bytes and responses use `nosniff` and `no-store`.

Direct file-link types are `CERSAI_AADHAAR`, `36`, `37`, `98`, and `DIGILOCKER_EAADHAAR`. The selected `kyc_doc` controls displayed type and allowed route; type is not inferred from URL naming. `AADHAAR_OTP` and `DIGILOCKER_AADHAAR_XML` use robust nested JSON/plain XML/base64 extraction from `other_metadata`, then parse XML and locally create a display PDF using the credential-free ReportLab dependency. The conversion is a readable rendering of extracted XML text, not a claim of reproducing the original document's layout or cryptographic validation. External conversion services/SSH are not used and no credentials are requested or stored. Missing metadata, unsafe XML/entity declarations, unavailable conversion dependencies, invalid content, and oversized content report truthful terminal states. XML/base64 is not put in HTML, errors, logs, or new workbook output fields.

Automated KYC viewer checks use only synthetic local PDF/image bytes and mocked fetches, including PDF/image success, each direct type, validation/error cases, lazy selection, metadata conversion/redaction, and no live S3/network.

## v1.2.9 KYC document validation compatibility

The KYC document viewer now validates PDF bytes with a non-strict PDF parser instead of requiring `%%EOF` to appear in the final 1 KB. This accepts valid PDFs returned by S3/CDN gateways with trailing bytes or a late EOF marker. For generic S3 MIME types such as `application/octet-stream`, `binary/octet-stream`, or `application/x-download`, the viewer safely infers PDF/PNG/JPEG/WEBP from the document signature and still validates the payload before displaying it. Unsupported MIME types, empty payloads, oversized responses, and genuinely malformed documents remain rejected.

Versioned output is `outputs/kyc_matching_output_v1.2.9.xlsx`.

## v1.3.0 reference-aligned review screens and sanction fallback scan

Versioned output is `outputs/kyc_matching_output_v1.4.0.xlsx`. User Details and KYC Details use the compact selected-user banner/avatar, status chips and keyboard hint; the user list stays in the left sidebar. User Details groups the existing source fields into Personal Information, Address Information, and Application Metadata with source labels and a right-side user-photo card. KYC Details separates the KYC source/selfie content from comparison results and displays existing score cards, status badges, and the existing accessible photo zoom controls. Selection still fetches records lazily; initial HTML does not embed record-level photo/document URLs or sensitive source values.

Sanction parsing scans pages one at a time, stopping once sanction date, reference, borrower name, Annexure, and the required terms phrase are found, or when the document ends / configured bound is reached. `SANCTION_MAX_TEXT_PAGES` defaults to 100 and is capped at 500 pages; `SANCTION_MAX_PAGE_TEXT_CHARS` defaults to 100,000 and is capped at 100,000 per page to bound extraction memory. It accepts common numeric and written date forms and normalizes valid dates to `DD/MM/YYYY`, and supports common reference/name label and next-line layouts. Partial results identify each missing required item (plus the scan range when the configured page bound truncates the scan). Background processing, URL-keyed cache, workbook schema, and source URL handling are unchanged.


## v1.4.0 operations review controls

Versioned output is `outputs/kyc_matching_output_v1.4.0.xlsx`. User Details, KYC Details, and Form each end with an Operations Review section containing a validated checkbox and multiline comments field; Summary intentionally has no review controls. KYC Details additionally places **Face match validated** immediately after the two selfie cards. All seven review values are included on every Enriched Data row as `user_details_validated`, `user_details_comments`, `kyc_details_validated`, `kyc_details_comments`, `kyc_face_match_validated`, `form_validated`, and `form_comments`; absent values begin as false/blank, and previously reviewed values are retained when a generated workbook is loaded again. `PATCH /api/records/<index>/review` (also accepts POST) updates only named review fields for that record, validates boolean/text types, rejects unknown keys and comments longer than 10,000 characters, and returns the updated review state. UI changes update the local selected-record cache immediately and are sent with a 350 ms debounce; workbook writes use the existing generation/state and atomic-write locks. Source columns and calculated fields remain intact, and downloads flush the latest pending state.


## v1.4.1 polished MITC and KYC document review

Versioned output is `outputs/kyc_matching_output_v1.4.1.xlsx`. The LP MITC Form uses a compact extraction-card stack beside a bounded, locally bundled PDF.js reader with page navigation, thumbnails, zoom and lazy canvas rendering. Source links are visibly abbreviated, and source-document bytes continue to use the local document routes. Integrity cards display evidence-based statuses; structural PDF signature presence is explicitly not claimed to be cryptographically verified. KYC Details keeps the aligned two-selfie source cards and optional KYC selfie-type badge, puts the existing operations face-match checkbox directly beneath the photos, then presents the KYC document card/viewer and KYC identity/personal/address cards. The right-side comparison area retains stored statuses and five score tiles, with per-field values and algorithm notes. Selected-record data and source links remain lazy. PDF.js is distributed with its Apache-2.0 license and the CMap/font notices.


## v1.4.2 All Documents and optional occupation

Versioned output is `outputs/kyc_matching_output_v1.4.2.xlsx`. Application Form `occupation_type` remains parsed and exported when present, but it is not required for completeness; an absent value stays blank and never appears in the missing-field warning list. The new **All Documents** tab is placed after Form and before the existing Summary. It fetches only the currently selected record and presents MITC, Application Form, Sanction Letter, KFS, GTC, KYC document, KYC selfie, and User selfie sources in a responsive card grid with source-column labels and truthful source/status availability. PDF/KYC viewing uses existing local secure document routes in a bounded scrollable preview, while supported direct sources can also open in a separate tab. Summary remains separate and has no operations controls. Initial HTML continues to omit record-level URLs; source links enter the browser only after the selected-record API request.


## v1.4.3 Form tab restoration

The Form tab remains the document-extraction and review screen with its prior MITC/KFS/Sanction Letter/Application Form/GTC controls. The All Documents tab remains visible after Form and before Summary, but is intentionally a blank reserved placeholder until its workflow is defined.


## v1.4.4 Form viewer compatibility

The Form tab no longer fails with `loadSourceDocument is not defined` when an older local checkout is missing `static/pdf-viewer.js`. The inline compatibility loader keeps the Form tab visible and uses the local Flask document route with the browser's native PDF viewer. Copying `static/pdf-viewer.js` and the bundled `static/vendor/pdfjs/` remains recommended for thumbnails, page navigation, and zoom.


## v1.4.5 KFS table boundaries and sanction source truthfulness

Versioned output is `outputs/kyc_matching_output_v1.4.5.xlsx`. KFS borrower identification number, name, and address are read from page 3 only. The parser stops at the next numbered borrower row, known next-field/PAN boundary, or Part 1 boundary; wrapped address lines are joined without importing row numbers or following content. Existing page-4 penal-charge checks and match calculations are unchanged. Sanction borrower names retain the complete name and remove only a leading honorific. A present-but-empty reference label yields `Not provided in source` and a specific blank-reference finding; an absent label is reported as not found. Existing bounded background scanning, annexure/terms, signature/logo checks, URL-keyed caching, and workbook output columns remain in place.


## v1.4.6 LP MITC numbered table boundaries

Versioned output is `outputs/kyc_matching_output_v1.4.6.xlsx`. LP MITC page 1 extraction now reads the Date, Customer identification number, and full Name of the Borrower values up to the next labeled/numbered row. This supports wrapped or flattened PDF text, excludes PAN/Effective Date/facility rows from those displayed fields, and keeps LP name matching against `kyc_name` at the existing threshold. Background fetching, URL caching, profile separation, structural signature/logo checks, and workbook export columns are unchanged.


## v1.4.7 KYC identity and address review layout

Versioned output is `outputs/kyc_matching_output_v1.4.7.xlsx`. KYC IDENTITY and PERSONAL DETAILS cards are more compact beneath the selected-record KYC document viewer. The KYC ADDRESSES section follows the preserved two-column source/comparison review panels and uses a wider, full-width two-card layout for Permanent and Current addresses, with source-field labels, readable wrapping, and bounded independent scrolling only for long values. Address text remains complete in the selected-record payload and is not supplemented with unrelated PDF text. The existing document viewer, selfie cards, face-match checkbox, stored comparisons/scores, lazy record loading, photo zoom, operations review controls, and mobile layout remain in place; no mappings or calculated scores changed.


## v1.4.8 vertically stacked KYC source cards

Versioned output is `outputs/kyc_matching_output_v1.4.8.xlsx`. In KYC Details, KYC Type/Mode/Status, KYC Name/Father Name/Date of Birth/Gender, and Permanent/Current Address each render as one-column vertical card groups. The source groups keep the existing card styles and source labels; the right-side Comparison Results content and ordering are unchanged. Address values remain complete, wrap at long tokens, and scroll independently within a bounded card. The selected-record source/document viewers, lazy loading, face-match checkbox, and operations-review controls are unchanged. The one-column layout applies at desktop, tablet, and mobile breakpoints.


## v1.4.10 page-one Sanction Letter identity extraction

Versioned output is `outputs/kyc_matching_output_v1.4.10.xlsx`. Sanction Letter date, reference, and borrower name are parsed from page 1 only; later-page text is never merged into these identity fields. A blank page-one reference label is exported as `Not provided in source`, while an absent label is left blank; the Form screen shows a specific reference finding. For the supplied page-one text, the date is `01/08/2026`, borrower is `SAHDEV SINGH` (full name with honorific removed and borrower parenthetical excluded), and reference is not provided in source. Annexure/terms scan, first-three-page logo evidence, and last-page signature evidence remain bounded and independent of identity extraction. Existing matching status/score, background work, URL cache, workbook columns, review controls, and privacy behavior are preserved.
