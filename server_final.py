#!/usr/bin/env python3
"""
LaTeX Resume Tailor - Final Server with Web OAuth
"""

import json
import os
import re
import uuid
import shutil
import logging
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import pickle
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ── In-memory job store ──────────────────────────────────────────────────────
JOB_STORE: dict = {}

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_DIR    = Path(__file__).parent
RESUME_FILE = BASE_DIR / "charan_reddy.tex"
HTML_FILE   = BASE_DIR / "index.html"
OUTPUT_DIR  = BASE_DIR / "outputs"
LOG_DIR     = BASE_DIR / "logs"

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

MAX_RESUME_CHARS    = 50000
MAX_JD_CHARS        = 50000
REQUEST_TIMEOUT     = 75
MODEL_NAME          = "claude-sonnet-4-20250514"
COMPILE_TIMEOUT_SEC = 120

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
OAUTH_FILE      = BASE_DIR / "credentials" / "oauth_client.json"
TOKEN_FILE      = BASE_DIR / "credentials" / "token.pickle"
SCOPES          = ["https://www.googleapis.com/auth/drive.file"]
APP_BASE_URL    = os.environ.get("APP_BASE_URL", "http://localhost:8080")

# ── Decode OAuth client from env var if present (for Render deployment) ────────
import base64
_oauth_b64 = os.environ.get("OAUTH_CLIENT_B64", "")
if _oauth_b64 and not OAUTH_FILE.exists():
    try:
        OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        OAUTH_FILE.write_bytes(base64.b64decode(_oauth_b64))
        print("OAuth client decoded from OAUTH_CLIENT_B64 env var.")
    except Exception as _e:
        print(f"Warning: Could not decode OAUTH_CLIENT_B64: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "server.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Prompts
# ══════════════════════════════════════════════════════════════════════════════

JD_ANALYSIS_PROMPT = """You are analyzing a job description for ATS optimization.
Rules:
1. Extract important phrases from the JD exactly or near-exactly where practical.
2. Classify each phrase into: technical, behavioral, managerial, domain, certification, other
3. Do not use hardcoded keyword lists.
4. Ignore trivial filler words.
5. Compare JD phrases against the resume and estimate presence/strength.
6. Identify weak or underrepresented categories.
7. Do not invent phrases not in the JD.

Return ONLY valid JSON:
{
  "keywords": [{"phrase": "","category": "","importance": "high|medium|low","presentInResume": true,"resumeCountEstimate": 0}],
  "mustCover": [],
  "weakCategories": [],
  "summary": ""
}"""

GENERATION_PROMPT = """You are a senior resume expert and ATS specialist.
Rewrite the LaTeX resume using the job description to achieve 90%+ ATS match.

STRICT CONSTRAINTS - DO NOT modify: personal details, education, company names, job titles, dates, LaTeX structure
ONLY modify: Skills section, Summary section, Experience bullet points

CRITICAL RULES:
1. 100% Keyword Coverage - every keyword must appear, exact wording only
2. Workflow-Based Bullets: Action → System → Method → Outcome
3. Role Differentiation - recent: architecture/scalability, previous: development/implementation
4. No repetition across roles, no AI-sounding words
5. NO NEW INFORMATION - only rephrase existing content
6. LaTeX must compile correctly

Respond ONLY with valid JSON:
{
  "atsScore": 0,
  "synthesizedTitle": "",
  "roleTransformation": "",
  "certificationChanges": "",
  "keywordsInjected": [],
  "missingKeywords": [],
  "categoryCoverage": {
    "technical": {"covered": 0, "total": 0},
    "behavioral": {"covered": 0, "total": 0},
    "managerial": {"covered": 0, "total": 0},
    "domain": {"covered": 0, "total": 0},
    "certification": {"covered": 0, "total": 0},
    "other": {"covered": 0, "total": 0}
  },
  "categoryScores": {"technical":0,"behavioral":0,"managerial":0,"domain":0,"certification":0,"other":0},
  "weakCategoriesAddressed": [],
  "assessmentSummary": "",
  "strengthsAdded": [],
  "latexCode": ""
}"""

# ══════════════════════════════════════════════════════════════════════════════
# AI helpers
# ══════════════════════════════════════════════════════════════════════════════

def read_base_resume() -> str:
    if not RESUME_FILE.exists():
        raise FileNotFoundError(f"Resume file not found: {RESUME_FILE}")
    text = RESUME_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("Resume file is empty.")
    return text[:MAX_RESUME_CHARS]

def parse_json_text(raw: str) -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean
    if clean.endswith("```"):
        clean = clean.rsplit("```", 1)[0]
    return json.loads(clean.strip())

def call_anthropic_json(system_prompt: str, user_msg: str) -> dict:
    payload = json.dumps({
        "model": MODEL_NAME, "max_tokens": 8000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": API_KEY,
                 "anthropic-version": "2023-06-01"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        api_data = json.loads(resp.read().decode("utf-8"))
    raw_text = "".join(b.get("text","") for b in api_data.get("content",[]) if isinstance(b,dict))
    return parse_json_text(raw_text)

def validate_jd_analysis(data: dict) -> dict:
    for key in ["keywords","mustCover","weakCategories","summary"]:
        if key not in data: raise ValueError(f"JD analysis missing: {key}")
    cleaned = []
    for item in data["keywords"]:
        if not isinstance(item, dict): continue
        phrase = str(item.get("phrase","")).strip()
        category = str(item.get("category","other")).strip().lower() or "other"
        importance = str(item.get("importance","medium")).strip().lower() or "medium"
        if not phrase: continue
        if category not in {"technical","behavioral","managerial","domain","certification","other"}: category="other"
        if importance not in {"high","medium","low"}: importance="medium"
        try: count = max(0, int(item.get("resumeCountEstimate",0)))
        except: count = 0
        cleaned.append({"phrase":phrase,"category":category,"importance":importance,
                        "presentInResume":bool(item.get("presentInResume",False)),"resumeCountEstimate":count})
    data["keywords"] = cleaned
    return data

def normalize_category_scores(value):
    cats = ["technical","behavioral","managerial","domain","certification","other"]
    if not isinstance(value, dict): value = {}
    return {c: max(0,min(100,int(value.get(c,0) or 0))) for c in cats}

def validate_generation_result(result: dict) -> dict:
    for key in ["atsScore","synthesizedTitle","roleTransformation","certificationChanges",
                "keywordsInjected","missingKeywords","categoryCoverage","categoryScores",
                "weakCategoriesAddressed","assessmentSummary","strengthsAdded","latexCode"]:
        if key not in result: raise ValueError(f"AI response missing: {key}")
    if not isinstance(result["latexCode"], str) or not result["latexCode"].strip():
        raise ValueError("latexCode must be non-empty.")
    try: result["atsScore"] = max(0, min(100, int(result["atsScore"])))
    except Exception as e: raise ValueError("atsScore must be numeric.") from e
    cats = ["technical","behavioral","managerial","domain","certification","other"]
    normalized = {}
    for cat in cats:
        item = result["categoryCoverage"].get(cat, {})
        if not isinstance(item, dict): item = {}
        try: covered = max(0, int(item.get("covered",0)))
        except: covered = 0
        try: total = max(0, int(item.get("total",0)))
        except: total = 0
        normalized[cat] = {"covered":covered,"total":total}
    result["categoryCoverage"] = normalized
    result["categoryScores"] = normalize_category_scores(result.get("categoryScores",{}))
    return result

def compute_missing_keywords(jd_analysis: dict, latex_code: str):
    text = (latex_code or "").lower()
    cats = ["technical","behavioral","managerial","domain","certification","other"]
    coverage = {c:{"covered":0,"total":0} for c in cats}
    missing = []
    for item in jd_analysis.get("keywords",[]):
        phrase = str(item.get("phrase","")).strip()
        category = str(item.get("category","other")).strip().lower() or "other"
        if not phrase: continue
        if category not in coverage: category = "other"
        coverage[category]["total"] += 1
        if phrase.lower() in text: coverage[category]["covered"] += 1
        else: missing.append(phrase)
    return missing, coverage

def derive_category_scores(coverage: dict):
    scores = {}
    for cat, item in coverage.items():
        total = int(item.get("total",0) or 0)
        covered = int(item.get("covered",0) or 0)
        scores[cat] = int(round((covered/total)*100)) if total > 0 else 0
    return normalize_category_scores(scores)

# ══════════════════════════════════════════════════════════════════════════════
# PDF compilation
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_name(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^\w\s-]","",s.strip())
    s = re.sub(r"[\s]+","_",s)
    return s[:maxlen] or "Unknown"

def build_pdf_filename(company: str, role: str) -> str:
    return f"CharanReddy_{sanitize_name(company)}_{sanitize_name(role)}.pdf"

def find_pdflatex() -> str:
    exe = shutil.which("pdflatex")
    if exe: return exe
    for c in [r"C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe",
               r"C:\texlive\2024\bin\windows\pdflatex.exe"]:
        if Path(c).exists(): return c
    raise RuntimeError("pdflatex not found. Install MiKTeX from https://miktex.org")

def save_and_compile(latex_code: str, job_id: str) -> Path:
    work_dir = OUTPUT_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    tex_path = work_dir / "document.tex"
    tex_path.write_text(latex_code, encoding="utf-8")
    pdflatex = find_pdflatex()
    cmd = [pdflatex,"-interaction=nonstopmode","-halt-on-error",
           "-no-shell-escape","-output-directory",str(work_dir),str(tex_path)]
    for pass_num in (1, 2):
        log.info("pdflatex pass %d for job %s", pass_num, job_id)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=COMPILE_TIMEOUT_SEC, cwd=str(work_dir))
        except subprocess.TimeoutExpired:
            raise RuntimeError("pdflatex timed out.")
        if result.returncode != 0:
            lines = [l for l in (result.stdout+result.stderr).splitlines() if l.startswith("!") or "Error" in l]
            raise RuntimeError("LaTeX compilation failed:\n" + "\n".join(lines)[:1200])
    pdf_path = work_dir / "document.pdf"
    if not pdf_path.exists(): raise RuntimeError("PDF not produced.")
    return pdf_path

def cleanup_aux_files(job_id: str) -> None:
    work_dir = OUTPUT_DIR / job_id
    if not work_dir.exists(): return
    for f in work_dir.iterdir():
        if f.suffix in {".aux",".log",".out",".toc",".tex"}:
            try: f.unlink()
            except OSError: pass

# ══════════════════════════════════════════════════════════════════════════════
# Google Drive OAuth
# ══════════════════════════════════════════════════════════════════════════════

def get_drive_credentials():
    try:
        from google.auth.transport.requests import Request
    except ImportError:
        return None
    if not TOKEN_FILE.exists(): return None
    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_FILE,"wb") as f: pickle.dump(creds,f)
        except Exception as e:
            log.warning("Token refresh failed: %s", e)
            return None
    return creds if (creds and creds.valid) else None

# Store code verifier between auth request and callback
_oauth_state = {}

def get_oauth_auth_url() -> str:
    from google_auth_oauthlib.flow import Flow
    if not OAUTH_FILE.exists():
        raise FileNotFoundError(f"oauth_client.json not found at {OAUTH_FILE}")
    flow = Flow.from_client_secrets_file(
        str(OAUTH_FILE), scopes=SCOPES,
        redirect_uri=f"{APP_BASE_URL}/oauth2callback",
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    # Save flow so callback can reuse it with same code verifier
    _oauth_state["flow"] = flow
    return auth_url


def exchange_oauth_code(code: str) -> None:
    from google_auth_oauthlib.flow import Flow

    # Reuse the same flow object that generated the auth URL
    flow = _oauth_state.pop("flow", None)

    if flow is None:
        # Fallback: create fresh flow (won't have code verifier — may fail)
        flow = Flow.from_client_secrets_file(
            str(OAUTH_FILE), scopes=SCOPES,
            redirect_uri=f"{APP_BASE_URL}/oauth2callback",
        )

    flow.fetch_token(code=code)
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    with open(TOKEN_FILE, "wb") as f:
        pickle.dump(flow.credentials, f)
    log.info("OAuth token saved.")

def upload_to_google_drive(pdf_path: Path, filename: str) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = get_drive_credentials()
    if not creds: raise RuntimeError("Not authorized. Visit /auth first.")
    service = build("drive","v3",credentials=creds,cache_discovery=False)
    meta = {"name": filename}
    if DRIVE_FOLDER_ID: meta["parents"] = [DRIVE_FOLDER_ID]
    media = MediaFileUpload(str(pdf_path), mimetype="application/pdf", resumable=False)
    uploaded = service.files().create(body=meta,media_body=media,fields="id").execute()
    file_id = uploaded["id"]
    service.permissions().create(fileId=file_id,body={"type":"anyone","role":"reader"}).execute()
    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    log.info("Uploaded to Drive: %s", link)
    return link


# ══════════════════════════════════════════════════════════════════════════════
# Background job processor
# ══════════════════════════════════════════════════════════════════════════════

def _process_job(job_id: str, jd: str, mode: str, company: str, role: str):
    """Runs in a background thread. Stores result in JOB_STORE."""
    try:
        jd    = jd[:MAX_JD_CHARS]
        latex = read_base_resume()

        jd_analysis = validate_jd_analysis(
            call_anthropic_json(JD_ANALYSIS_PROMPT,
                f"LATEX RESUME:\n{latex}\n\nJOB DESCRIPTION:\n{jd}\n")
        )

        result = validate_generation_result(
            call_anthropic_json(GENERATION_PROMPT,
                f"Optimization mode: {mode}\nTarget Company: {company}\nTarget Role: {role}\n\n"
                f"LATEX RESUME:\n{latex}\n\nJOB DESCRIPTION:\n{jd}\n\n"
                f"JD ANALYSIS:\n{json.dumps(jd_analysis,ensure_ascii=False)}")
        )

        missing, recomputed_cov = compute_missing_keywords(jd_analysis, result["latexCode"])
        result["missingKeywords"]   = missing
        result["categoryCoverage"]  = recomputed_cov
        result["categoryScores"]    = derive_category_scores(recomputed_cov)
        result["jdAnalysisSummary"] = jd_analysis["summary"]
        result["jdWeakCategories"]  = jd_analysis["weakCategories"]
        result["jdMustCover"]       = jd_analysis["mustCover"]

        pdf_name = build_pdf_filename(company, role)

        try:
            save_and_compile(result["latexCode"], job_id)
            result["download_url"] = f"/download/{job_id}/{pdf_name}"
            result["pdf_filename"] = pdf_name
            cleanup_aux_files(job_id)
        except RuntimeError as exc:
            log.warning("PDF compilation failed: %s", exc)
            result["download_url"]    = None
            result["pdf_filename"]    = None
            result["compile_warning"] = str(exc)

        if result.get("download_url"):
            creds = get_drive_credentials()
            if creds:
                try:
                    drive_link = upload_to_google_drive(
                        OUTPUT_DIR / job_id / "document.pdf", pdf_name)
                    result["drive_link"] = drive_link
                except Exception as exc:
                    log.warning("Drive upload failed: %s", exc)
                    result["drive_warning"] = str(exc)
            else:
                result["drive_warning"] = "not_authorized"

        JOB_STORE[job_id] = {"status": "done", "result": result, "error": None}
        log.info("Job %s completed successfully.", job_id)

    except Exception as exc:
        log.exception("Job %s failed: %s", job_id, exc)
        JOB_STORE[job_id] = {"status": "error", "result": None, "error": str(exc)}

# ══════════════════════════════════════════════════════════════════════════════
# HTTP Handler
# ══════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info("[%s] %s", self.address_string(), format % args)

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")

    def do_OPTIONS(self):
        self.send_response(200); self.send_cors(); self.end_headers()

    def _do_GET_original(self):
        if self.path == "/auth":
            try:
                auth_url = get_oauth_auth_url()
                self.send_response(302)
                self.send_header("Location", auth_url)
                self.send_cors(); self.end_headers()
            except Exception as e:
                self._json_error(500, f"Could not build auth URL: {e}")
            return

        if self.path.startswith("/oauth2callback"):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code   = params.get("code",[None])[0]
            if not code:
                self._html_page("OAuth Error","No code received from Google.")
                return
            try:
                exchange_oauth_code(code)
                self._html_page(
                    "✅ Google Drive Connected!",
                    "Your Google account is now linked. Close this tab and return to the app. "
                    "Resumes will now upload to Drive automatically."
                )
            except Exception as e:
                log.error("OAuth callback error: %s", e)
                self._html_page("OAuth Error", f"Failed to connect: {e}")
            return

        if self.path == "/drive-status":
            creds = get_drive_credentials()
            self._json_ok({"connected": creds is not None}); return

        if self.path == "/resume-status":
            exists = RESUME_FILE.exists()
            preview = ""
            if exists:
                try: preview = RESUME_FILE.read_text(encoding="utf-8")[:300]
                except: pass
            self._json_ok({"saved":exists,"preview":preview,"path":str(RESUME_FILE)}); return

        if self.path.startswith("/download/"):
            self._serve_pdf(self.path); return

        if self.path == "/":
            if not HTML_FILE.exists():
                self.send_response(404); self.end_headers()
                self.wfile.write(b"index.html not found"); return
            content = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_cors(); self.end_headers()
            self.wfile.write(content); return

        self.send_response(404); self.send_cors(); self.end_headers()

    def _serve_pdf(self, path: str):
        m = re.fullmatch(r"/download/([0-9a-f]{32})/([^/]+\.pdf)", path)
        if not m:
            self.send_response(400); self.send_cors(); self.end_headers(); return
        job_id, filename = m.group(1), m.group(2)
        pdf_path = OUTPUT_DIR / job_id / "document.pdf"
        if not pdf_path.exists():
            self.send_response(404); self.send_cors(); self.end_headers(); return
        data = pdf_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type","application/pdf")
        self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
        self.send_header("Content-Length",str(len(data)))
        self.send_cors(); self.end_headers()
        self.wfile.write(data)

    def _html_page(self, title: str, message: str):
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title>
<style>
body{{font-family:sans-serif;background:#0B0C0F;color:#F0EEE8;
     display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{background:#13141A;border:1px solid rgba(255,255,255,0.08);border-radius:14px;
      padding:40px;max-width:500px;text-align:center}}
h1{{color:#C8F060;font-size:24px;margin-bottom:16px}}
p{{color:#8A8892;line-height:1.6}}a{{color:#C8F060}}
</style></head><body><div class="box">
<h1>{title}</h1><p>{message}</p>
<p><a href="/">← Back to Resume Tailor</a></p>
</div></body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_cors(); self.end_headers()
        self.wfile.write(html)

    def do_POST(self):
        length = int(self.headers.get("Content-Length",0))
        body   = self.rfile.read(length)
        if self.path == "/tailor": self._handle_tailor(body)
        else: self.send_response(404); self.send_cors(); self.end_headers()

    def do_GET(self, *args, **kwargs):
        # Status polling endpoint
        if self.path.startswith("/status/"):
            job_id = self.path[len("/status/"):]
            self._handle_status(job_id); return
        self._do_GET_original()

    def _handle_tailor(self, body: bytes):
        """Start async job and return job_id immediately to avoid proxy timeout."""
        try:
            if not API_KEY or API_KEY == "YOUR_ANTHROPIC_KEY_HERE":
                self._json_error(500,"Missing ANTHROPIC_API_KEY."); return

            payload = json.loads(body or b"{}")
            jd      = str(payload.get("jd","")).strip()
            mode    = str(payload.get("mode","full")).strip() or "full"
            company = str(payload.get("company","")).strip() or "Company"
            role    = str(payload.get("role","")).strip() or "Role"

            if not jd: self._json_error(400,"Job description is required."); return

            job_id = uuid.uuid4().hex
            JOB_STORE[job_id] = {"status": "pending", "result": None, "error": None}

            # Start background thread so we return immediately
            t = threading.Thread(target=_process_job,
                                 args=(job_id, jd, mode, company, role),
                                 daemon=True)
            t.start()

            self._json_ok({"job_id": job_id})

        except json.JSONDecodeError as e:
            self._json_error(400, f"Invalid JSON: {e}")
        except Exception as e:
            log.exception("Unexpected error starting job")
            self._json_error(500, f"Unexpected error: {e}")

    def _handle_status(self, job_id: str):
        """Poll endpoint — returns job status and result when done."""
        job = JOB_STORE.get(job_id)
        if not job:
            self._json_error(404, "Job not found."); return
        self._json_ok(job)

    def _json_ok(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_cors(); self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code, message):
        body = json.dumps({"error":message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_cors(); self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("="*60)
    print("  LaTeX Resume Tailor — Final Server")
    print("="*60)
    print(f"  Resume:   {RESUME_FILE}")
    print(f"  Base URL: {APP_BASE_URL}")
    print()
    print("  Base resume:", "✓ found" if RESUME_FILE.exists() else "✗ missing")
    try: find_pdflatex(); print("  pdflatex:   ✓ found")
    except RuntimeError as e: print(f"  pdflatex:   ✗ {e}")
    print("  OAuth file:", "✓ found" if OAUTH_FILE.exists() else "✗ missing — place oauth_client.json in credentials/")
    creds = get_drive_credentials()
    if creds: print("  Drive auth: ✓ connected")
    else:
        print("  Drive auth: ✗ not connected")
        print(f"  → Visit {APP_BASE_URL}/auth to connect Google Drive")
    print()
    print(f"  Running at 0.0.0.0:{port}")
    print("  Press Ctrl+C to stop.")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nServer stopped.")