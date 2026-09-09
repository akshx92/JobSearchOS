import os
import io
import glob
import json
import re
import time
from google import genai
from google.genai.errors import APIError
from dotenv import load_dotenv
from pypdf import PdfReader

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Verified working model name as of writing. If Google renames/retires this,
# update it here only — everything else references this constant.
MODEL_NAME = "gemini-3.6-flash"


def load_cv_from_bytes(file_bytes, filename=""):
    """Primary CV loading path: extracts text from raw file bytes fetched
    from the database (cv_versions.file_content). This is what makes the CV
    survive reboots/redeploys — it's stored in Postgres, not on local disk,
    which is wiped every time the app container restarts."""
    if not file_bytes:
        return None
    name_lower = (filename or "").lower()
    try:
        if name_lower.endswith(".txt"):
            return file_bytes.decode("utf-8", errors="ignore")
        # Default to PDF parsing (also covers unknown/missing extensions,
        # since PDF is the only upload type the UI currently accepts).
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
        return text if text.strip() else None
    except Exception:
        return None


def load_cv(cv_path=None):
    """
    Legacy file-path-based loader, kept for local development/testing only.
    Production scans should use load_cv_from_bytes() instead, since local
    files don't survive cloud reboots/redeploys.
    """
    if cv_path and os.path.isfile(cv_path):
        return _read_cv_file(cv_path)

    search_patterns = ["*resume*.pdf", "*resume*.txt", "*CV*.pdf", "*CV*.txt", "*cv*.pdf", "*cv*.txt"]
    matching_files = []
    for pattern in search_patterns:
        for f in glob.glob(pattern):
            if os.path.isfile(f):
                matching_files.append(f)
    matching_files = list(set(matching_files))
    if not matching_files:
        return None
    return _read_cv_file(matching_files[0])


def _read_cv_file(path):
    if path.lower().endswith(".txt"):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    elif path.lower().endswith(".pdf"):
        try:
            reader = PdfReader(path)
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
            return text
        except Exception:
            return None
    return None


def evaluate_job_match(job_description, cv_text):
    """
    Evaluates a job description against pre-loaded CV text (pass the CV text
    in once per scan run, not per job — avoids re-reading the file hundreds
    of times). Returns a dict, always with an 'error' key (None on success)
    so the caller can log real failures instead of them being silently masked.
    """
    if not cv_text:
        return {
            "match_score": 0, "alignment": "Unknown",
            "rationale": "CV could not be loaded.",
            "required_experience_min": None, "required_experience_max": None,
            "error": "CV_NOT_LOADED",
        }

    if not job_description or len(job_description.strip()) < 15:
        job_description = "General professional role requiring cross-functional collaboration and execution."

    prompt = f"""
You are an expert AI Career Coach and Recruiter.
Analyze the following Candidate CV against the provided Job Description (JD).

CANDIDATE CV:
{cv_text}

JOB DESCRIPTION:
{job_description}

Respond with ONLY a valid JSON object — no markdown, no backticks, no extra text —
in exactly this shape:
{{
  "match_score": <integer 0-100>,
  "experience_alignment": "<Strong, Moderate, or Weak>",
  "required_experience_min": <integer years required by the JD, or null if not stated>,
  "required_experience_max": <integer years required by the JD, or null if not stated>,
  "rationale": "<concise 2-sentence explanation>"
}}
"""

    max_retries = 2
    delay = 10

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            raw_text = response.text.strip()
            raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
            data = json.loads(raw_text)

            return {
                "match_score": int(data.get("match_score", 50)),
                "alignment": data.get("experience_alignment", "Moderate"),
                "rationale": data.get("rationale", "Evaluated successfully via AI."),
                "required_experience_min": data.get("required_experience_min"),
                "required_experience_max": data.get("required_experience_max"),
                "error": None,
            }

        except APIError as e:
            error_str = str(e)
            is_transient = any(code in error_str for code in ["429", "503", "500", "502", "504"])
            if is_transient:
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                label = "RATE_LIMIT" if "429" in error_str else "SERVER_UNAVAILABLE"
                rationale = ("API rate limit reached after retries." if "429" in error_str
                             else "Gemini's servers were unavailable after retries (high demand) — try this job again later.")
                return {
                    "match_score": 0, "alignment": "Unknown",
                    "rationale": rationale,
                    "required_experience_min": None, "required_experience_max": None,
                    "error": f"{label}: {e}",
                }
            # Not transient (e.g. 404 model not found, 400 bad request, 403 auth) —
            # retrying won't help, so fail fast with an accurate label instead of
            # burning through the backoff loop and calling it a rate limit.
            return {
                "match_score": 0, "alignment": "Unknown",
                "rationale": "AI evaluation failed due to an API error (not a rate limit).",
                "required_experience_min": None, "required_experience_max": None,
                "error": f"API_ERROR: {e}",
            }
        except (json.JSONDecodeError, ValueError) as e:
            return {
                "match_score": 0, "alignment": "Unknown",
                "rationale": "AI response could not be parsed as JSON.",
                "required_experience_min": None, "required_experience_max": None,
                "error": f"PARSE_ERROR: {e}",
            }
        except Exception as e:
            return {
                "match_score": 0, "alignment": "Unknown",
                "rationale": "Evaluation failed due to an unexpected error.",
                "required_experience_min": None, "required_experience_max": None,
                "error": f"UNEXPECTED_ERROR: {e}",
            }

    return {
        "match_score": 0, "alignment": "Unknown",
        "rationale": "Evaluation timed out after retries.",
        "required_experience_min": None, "required_experience_max": None,
        "error": "TIMEOUT",
    }
