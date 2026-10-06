# Documents

HyprChat's **Documents** tool reads, creates, edits and exports uploaded Word,
PowerPoint and Excel files. Enable it in **Settings → Connections → Documents**, then
select Documents in the chat tool menu or attach an Office file. Delivered files
appear in chat and Artifact Studio's **Word**, **Slides** and **Sheets** filters.
The eye button on a chat file card opens a resizable preview beside chat, with PDF
rendering, export controls and worksheet/range inspection. Missing previews render
on demand. The pane follows newly saved revisions of the selected document; unrelated
files do not replace it. On mobile, close the full-screen preview to return to chat.
Artifact Studio retains its existing detail previews.

Try:

- Attach a DOCX: “Change the introduction to mention October. Keep the formatting.”
- “Create a five-slide PowerPoint with speaker notes summarizing these findings.”
- Attach an XLSX: “Read Budget!A1:D20, change hosting to 45, and export Budget as CSV.”
- “Create a Word report with a heading, summary and comparison table.”
- Attach a PNG/JPEG with Documents enabled: “Use this image in a new report.”

## Supported operations

| Format | Read and create | Targeted edits | Export |
| --- | --- | --- | --- |
| DOCX | Paragraphs, headings, tables, images, headers/footers | Text replacements, text styling, images, page size/margins | PDF, extracted TXT/Markdown |
| PPTX | Slides, text, tables, images, notes, simple charts | Text/style, slide order, shape position/size, images, embedded bar/line/pie chart data | PDF, extracted TXT/Markdown |
| XLSX | Worksheets, cells, formulas, tables, styles, simple charts | Cell values/formulas/styles; chart data through source cells | PDF, one named sheet as CSV |
| DOC/PPT/XLS | Normalize through LibreOffice to the modern equivalent | Edit the normalized copy | Modern equivalent and applicable exports above |

Google Docs/Slides/Sheets can be used by downloading an Office file, working on it
in HyprChat, then uploading the result back to Google. Direct Google Drive or
Microsoft account connections, live synchronization and embedded Office editors
are outside this version. PDF-to-Office reconstruction and automatic document-to-slide
transformations are also outside the conversion tool.

## Preservation and validation

Original uploads are immutable. Edits produce linked revisions with unique storage
names; repeated filenames do not overwrite each other. Document jobs display compact activity and Download/Preview controls rather than the
Daedalus project timeline. Failed operations remain visible even when a later read succeeds.
File controls recover from persisted runs after refresh. The tool reads before editing
and requires the source SHA-256, so stale edits fail. Existing OOXML edits patch selected
XML/package members and retain untouched members, including unknown features.
Unsupported structural changes fail rather than silently rebuilding an uploaded file.

The worker checks file structure; tests also reopen outputs with independent libraries. This is separate from
visual correctness: inspect the preview before using an important deliverable.
LibreOffice pagination, fonts and advanced features can differ from Microsoft Office.
Native-format conversion intentionally re-saves through LibreOffice and reports this
limitation. Preview failure retains a valid downloadable Office file with a warning.

Formula inspection shows source formulas and cached values. Targeted edits request
recalculation when Excel opens the file; they do not fabricate fresh cached results.
CSV exports use cached values, which can be empty or stale. LibreOffice PDF rendering
calculates its own copy. Array/shared/data-table formula edits, protected sheets,
calculation-chain workbooks and externally linked PowerPoint chart data have conservative
editing restrictions. Re-saving a copy may normalize some unsupported structures, but
can change advanced Office features.

Input is limited to 50 MB. Macro-enabled or encrypted files and unsafe ZIP/XML
packages are rejected. LibreOffice runs with macros disabled and external link updates
disabled, inside a network-isolated bubblewrap process with only its job directory
writable. Jobs execute serially with a five-minute queue wait and five-minute execution
limit. Stop terminates the worker process group. Graceful backend shutdown cancels API
jobs; startup marks interrupted runs failed after an abrupt exit. Partial results are
not published. Completed artifacts survive restarts.

Artifact records and binary downloads use the active HyprChat user scope. Managed
Office files are unavailable through the older filename-only download/archive endpoints.
Backend originals and revisions follow the existing artifact retention policy. Private
Codebox staging is removed by systemd-tmpfiles after one day.

## Installation and maintenance

The backend uses the standard-library `document_formats.py` reader. Heavy Office
libraries and LibreOffice run on Codebox in a separate virtual environment, leaving
the coding worker's environment unchanged.

Copy these files onto Codebox under `/opt/hyprchat-documents/`:

- `backend/document_formats.py`
- `backend/document_runtime.py`
- `backend/document_worker.py`
- `backend/document-requirements.txt`
- `scripts/install-documents.sh`

Run the installation script there as root. It installs LibreOffice Writer/Calc/Impress,
bubblewrap, fonts and the dedicated Python dependencies. Confirm readiness with:

```sh
/opt/hyprchat-documents/venv/bin/python /opt/hyprchat-documents/document_worker.py health
```

The staging path is `/root/hyprchat-documents/jobs`, within Codebox's existing allowed
upload roots. Runtime code lives in `/opt/hyprchat-documents`; it is read-only inside
jobs. `deploy_monitor.py` watches the worker files and sends them to Codebox; it also
copies the shared format reader to HyprChat. Re-run the installation script after
changing the dependency manifest. Deploy the backend and rebuilt frontend normally,
then enable Documents after its readiness check succeeds. Fresh installations default
to disabled.

Creation content is validated before a worker starts. Native and fallback calls share
the nested content schema. Word/PDF exports of chat answers can instead supply
`markdown` with `format="docx"`, omitting `content`. Basic headings, bold text, inline
code and pipe tables become Word content; other syntax remains readable text. Code
blocks and later sections are retained. This avoids re-encoding reports as nested JSON.
Valid JSON-encoded objects are accepted once; malformed
content gets a field error and one corrective attempt per tool per chat turn. A second
argument failure ends the request with a visible error. A failed creation cannot be
substituted with a successful read/normalization result. For a new
PDF report, the Word creation result already includes a downloadable PDF when rendering
succeeds; for an existing Office file, use conversion to PDF.

The five tools are `document_read`, `document_create`, `document_edit`,
`document_convert`, and `document_preview`. Contracts/examples live in
`backend/document_tools.py`; both native provider tools and the local text fallback
use the same handlers. They are separate from CodeAgent and Daedalus.

API clients upload through `POST /api/documents/upload`, submit jobs through
`POST /api/documents/jobs`, poll `GET /api/runs/{run_id}`, and stop through
`POST /api/runs/{run_id}/cancel`. Downloads use `/api/documents/files/{artifact_id}`.
Use the existing HyprChat user/session authentication on all these endpoints.

## Verification

```sh
python -m pytest backend/tests/test_documents_runtime.py backend/tests/test_documents_service.py -q
cd frontend
node --test src/documentFiles.test.js src/chatSend.test.js
npm run build
```

Backend tests require the document dependency manifest plus the normal test dependencies.
They cover preservation of formatting/images/unknown XML, independent library reopening,
chart edits, formula ranges, malformed files, stale hashes, ownership, cancellation,
shutdown and transactional publication failure. Live qualification should additionally
exercise Codebox, actual LibreOffice output, browser previews/downloads, local native
and fallback chat models, and restart behavior. Unit tests alone do not validate layout
or deployment.
