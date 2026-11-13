import os
import re
import json
import time
import base64
import logging
import threading
import uuid
import sqlite3
import subprocess
import tempfile
import shutil
from pathlib import Path
from io import BytesIO

import requests
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    session,
    send_file
)

# ==========================================
# ADAMI AI VIDEO STUDIO - SINGLE MP4 VERSION
# ==========================================

app = Flask(__name__)

app.secret_key = os.environ["FLASK_SECRET_KEY"]

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    MAX_CONTENT_LENGTH=7 * 1024 * 1024
)

logging.basicConfig(level=logging.INFO)

# ==========================================
# SECRET CONFIGURATION
# ==========================================

RUNWAY_API_KEY = os.environ.get("RUNWAY_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
ADMIN_TOKEN = os.environ.get("ADAMI_ADMIN_TOKEN", "")
RUNWAY_VERSION = "2024-11-06"
RUNWAY_MODEL = os.environ.get("RUNWAY_MODEL", "gen4.5")
RUNWAY_BASE_URL = "https://api.dev.runwayml.com/v1"

# ElevenLabs for Naija Voice
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get(
    "ELEVENLABS_VOICE_ID",
    "GVstkJ8LC6TRgAgOZ5IA"
)
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# ==========================================
# PUBLIC GENERATION LIMIT - LEAVE AS 3 DAYS - DO NOT CHANGE
# ==========================================

PUBLIC_VIDEO_SECONDS = 10
PUBLIC_COOLDOWN_SECONDS = 3 * 24 * 60 * 60

# ==========================================
# ADAMI PERMANENT STORAGE
# ==========================================

DATA_DIRECTORY = Path(os.environ.get("ADAMI_DATA_DIR", "/data"))

try:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    test_file = DATA_DIRECTORY / ".adami_storage_test"
    test_file.write_text("storage_ok", encoding="utf-8")
    test_file.unlink()
    logging.info("ADAMI persistent storage is ready.")
except Exception as error:
    logging.critical("ADAMI persistent storage is unavailable: %s", error)
    raise RuntimeError("ADAMI cannot start because persistent storage is unavailable.") from error

# ==========================================
# ADAMI PERMANENT DATABASE
# ==========================================

DATABASE_FILE = DATA_DIRECTORY / "adami.sqlite3"

def get_db_connection():
    connection = sqlite3.connect(str(DATABASE_FILE), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection

def initialize_database():
    connection = get_db_connection()
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT DEFAULT '',
            fix_credits INTEGER NOT NULL DEFAULT 0,
            account_status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            programming_language TEXT NOT NULL,
            project_files TEXT NOT NULL DEFAULT '[]',
            project_summary TEXT DEFAULT '',
            project_status TEXT NOT NULL DEFAULT 'saved',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            project_id TEXT,
            reference TEXT NOT NULL UNIQUE,
            package_name TEXT NOT NULL,
            amount_kobo INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'NGN',
            fix_credits INTEGER NOT NULL DEFAULT 0,
            payment_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            verified_at TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            payment_id TEXT,
            transaction_type TEXT NOT NULL,
            credits_changed INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            description TEXT DEFAULT '',
            reference TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (payment_id) REFERENCES payments(id)
        );
        CREATE INDEX IF NOT EXISTS idx_projects_customer ON projects(customer_id);
        CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
        CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(payment_status);
        CREATE INDEX IF NOT EXISTS idx_credit_customer ON credit_transactions(customer_id);
        """)
        connection.commit()
        app.logger.info("ADAMI permanent database initialized successfully.")
    finally:
        connection.close()

initialize_database()

USAGE_FILE = DATA_DIRECTORY / "adami_usage.json"
usage_lock = threading.Lock()

def load_usage():
    try:
        if USAGE_FILE.exists():
            with open(USAGE_FILE, "r") as file:
                return json.load(file)
    except Exception:
        app.logger.exception("Could not read usage file")
    return {}

def save_usage(data):
    temporary_file = USAGE_FILE.with_suffix(".tmp")
    with open(temporary_file, "w") as file:
        json.dump(data, file)
    temporary_file.replace(USAGE_FILE)

def get_user_identifier():
    return request.remote_addr or "unknown"

def check_public_limit():
    identifier = get_user_identifier()
    current_time = int(time.time())
    with usage_lock:
        usage = load_usage()
        last_generation = int(usage.get(identifier, 0))
        elapsed = current_time - last_generation
        if elapsed < PUBLIC_COOLDOWN_SECONDS:
            remaining = PUBLIC_COOLDOWN_SECONDS - elapsed
            return False, remaining
        return True, 0

def record_public_generation():
    identifier = get_user_identifier()
    with usage_lock:
        usage = load_usage()
        usage[identifier] = int(time.time())
        save_usage(usage)

# ==========================================
# RUNWAY HEADERS
# ==========================================

def runway_headers():
    return {
        "Authorization": f"Bearer {RUNWAY_API_KEY}",
        "Content-Type": "application/json",
        "X-Runway-Version": RUNWAY_VERSION
    }

# ==========================================
# IMAGE VALIDATION - FIXED FOR 0.447
# ==========================================

def validate_image(image_data):
    if not isinstance(image_data, str):
        raise ValueError("Please upload a character photo.")
    match = re.match(r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$", image_data, re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("Invalid photo. Upload a JPG, PNG or WEBP image.")
    image_format = match.group(1).lower()
    encoded_data = match.group(2)
    try:
        decoded = base64.b64decode(encoded_data, validate=True)
    except Exception:
        raise ValueError("The uploaded photo is damaged.")
    if len(decoded) > 5 * 1024 * 1024:
        raise ValueError("Your photo must be 5MB or smaller.")
    if len(decoded) < 100:
        raise ValueError("The uploaded photo is too small.")
    if image_format == "jpg":
        image_format = "jpeg"
    try:
        from PIL import Image
        img = Image.open(BytesIO(decoded))
        img = img.convert("RGB")
        w, h = img.size
        ratio = w / h if h > 0 else 0
        target_ratio = 720 / 1280
        if ratio < 0.55 or ratio > 0.60:
            if ratio < target_ratio:
                new_h = int(w / target_ratio)
                top = max(0, (h - new_h) // 2)
                img = img.crop((0, top, w, top + new_h))
            else:
                new_w = int(h * target_ratio)
                left = max(0, (w - new_w) // 2)
                img = img.crop((left, 0, left + new_w, h))
        img = img.resize((720, 1280))
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=88)
        new_encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{new_encoded}"
    except ImportError:
        return f"data:image/{image_format};base64,{encoded_data}"
    except Exception:
        app.logger.exception("Image auto-crop failed")
        return f"data:image/{image_format};base64,{encoded_data}"

# ==========================================
# BUILD PROMPT - CAPPED 950
# ==========================================

def build_prompt(data):
    style = str(data.get("style", "realistic human cartoon")).strip()[:150]
    script = str(data.get("script", "")).strip()[:500]
    direction = str(data.get("direction", "")).strip()[:250]
    emotion = str(data.get("emotion", "Natural")).strip()[:60]
    voice = str(data.get("voice", "main-naija-pidgin")).strip()[:60]
    prompt = (
        "Create a cinematic Nigerian/African animated video "
        "using the uploaded image as the first frame. "
        f"Style: {style}. "
        f"Story: {script}. "
        f"Direction: {direction}. "
        f"Emotion: {emotion}. "
        f"Voice style: {voice}. "
        "Preserve the person's identity, face, hairstyle, "
        "skin tone and clothing. Use natural facial expressions, "
        "blinking, body movement and believable gestures. "
        "Use smooth cinematic camera movement. "
        "Keep the character visually consistent throughout. "
        "Avoid distorted faces, extra fingers and unnatural motion."
    )
    return prompt[:950]

# ==========================================
# RUNWAY TASK
# ==========================================

def create_runway_task(image_uri, prompt, duration):
    payload = {
        "model": RUNWAY_MODEL,
        "promptImage": image_uri,
        "promptText": prompt,
        "ratio": "720:1280",
        "duration": duration,
        "position": "first"
    }
    response = requests.post(f"{RUNWAY_BASE_URL}/image_to_video", headers=runway_headers(), json=payload, timeout=60)
    if response.status_code not in (200, 201):
        try:
            details = response.json()
        except Exception:
            details = response.text[:1000]
        app.logger.error("Runway task creation failed: %s", details)
        raise RuntimeError(f"Runway rejected the request: {details}")
    result = response.json()
    task_id = result.get("id")
    if not task_id:
        raise RuntimeError("Runway did not return a task ID.")
    return task_id

def wait_for_runway_video(task_id):
    maximum_wait = 360
    start_time = time.time()
    while time.time() - start_time < maximum_wait:
        response = requests.get(f"{RUNWAY_BASE_URL}/tasks/{task_id}", headers=runway_headers(), timeout=30)
        if response.status_code != 200:
            raise RuntimeError("Could not check Runway generation status: " + response.text[:500])
        result = response.json()
        status = result.get("status", "").upper()
        app.logger.info("Runway task %s status: %s", task_id, status)
        if status == "SUCCEEDED":
            output = result.get("output")
            if isinstance(output, list) and output:
                return output[0]
            if isinstance(output, str):
                return output
            raise RuntimeError("Runway finished but returned no video URL.")
        if status in ("FAILED", "CANCELLED", "THREW_EXCEPTION"):
            raise RuntimeError("Runway video generation failed: " + str(result.get("failure", result.get("error", status))))
        time.sleep(5)
    raise RuntimeError("Video generation is taking too long. Please try again.")

# ==========================================
# FFMPEG + ELEVENLABS - SINGLE MP4 - FIXED LOUD VOICE
# ==========================================

def get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"

def download_file(url, dest_path):
    r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return dest_path

def get_media_duration(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        ffprobe = str(Path(get_ffmpeg_path()).parent / "ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0

def generate_naija_voice(script_text, output_mp3_path):
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        app.logger.warning("ElevenLabs not configured (missing API key or voice ID) — skipping voice")
        return None
    if not script_text or not script_text.strip():
        app.logger.warning("Empty script — skipping voice generation")
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"
    }
    payload = {
        "text": script_text[:4000],
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.80,
            "style": 0.35,
            "use_speaker_boost": True
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        if r.status_code != 200:
            app.logger.error("ElevenLabs failed (%s): %s", r.status_code, r.text[:800])
            return None
        with open(output_mp3_path, "wb") as f:
            f.write(r.content)
        if Path(output_mp3_path).stat().st_size < 1000:
            app.logger.error("ElevenLabs returned almost empty audio file")
            return None
        app.logger.info("ElevenLabs voice generated successfully → %s", output_mp3_path)
        return output_mp3_path
    except Exception:
        app.logger.exception("ElevenLabs request failed")
        return None

def concat_and_add_voice(video_urls, script_text, final_output_path, target_duration):
    ffmpeg = get_ffmpeg_path()
    tmpdir = Path(tempfile.mkdtemp())
    try:
        clip_paths = []
        for i, url in enumerate(video_urls):
            p = tmpdir / f"clip_{i:02d}.mp4"
            download_file(url, p)
            clip_paths.append(p)
        concat_file = tmpdir / "concat.txt"
        with open(concat_file, "w") as f:
            for p in clip_paths:
                f.write(f"file '{p.as_posix()}'\n")
        joined_path = tmpdir / "joined.mp4"
        cmd_concat = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined_path)]
        subprocess.run(cmd_concat, check=True, capture_output=True, timeout=300)
        video_duration = get_media_duration(joined_path)
        app.logger.info("Joined video duration: %.2fs (target was %ss)", video_duration, target_duration)
        voice_mp3 = tmpdir / "voice.mp3"
        voice_path = generate_naija_voice(script_text, voice_mp3)
        if not voice_path or not Path(voice_path).exists():
            app.logger.warning("No voice available — returning silent video")
            subprocess.run([ffmpeg, "-y", "-i", str(joined_path), "-c", "copy", str(final_output_path)], check=True, capture_output=True, timeout=120)
            return str(final_output_path)
        voice_duration = get_media_duration(voice_path)
        app.logger.info("Voice duration: %.2fs", voice_duration)

        # FIXED: Voice LOUD + Normalized + 0.4s delay for better lip look
        cmd_final = [
            ffmpeg, "-y",
            "-i", str(joined_path),
            "-i", str(voice_path),
            "-filter_complex",
            f"[1:a]adelay=400|400,apad=pad_dur={max(0, video_duration - voice_duration + 0.4)},atrim=0:{video_duration},volume=3.0,loudnorm=I=-14:TP=-1:LRA=11[a]",
            "-map", "0:v:0",
            "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(final_output_path)
        ]
        result = subprocess.run(cmd_final, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            app.logger.error("ffmpeg mux failed:\n%s", result.stderr[-1500:])
            raise RuntimeError("ffmpeg voice mux failed")
        probe = subprocess.run([ffmpeg, "-i", str(final_output_path), "-f", "null", "-"], capture_output=True, text=True, timeout=30)
        if "Audio:" not in probe.stderr:
            app.logger.error("Final MP4 has NO audio stream!")
            raise RuntimeError("Voice was generated but failed to mux into the final video")
        app.logger.info("Final single MP4 with LOUD voice ready: %s", final_output_path)
        return str(final_output_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ==========================================
# HOMEPAGE
# ==========================================

@app.route("/")
def home():
    return send_from_directory(".", "index.html")

# ==========================================
# ADMIN UNLOCK
# ==========================================

@app.route("/api/cartoon/admin/unlock", methods=["POST"])
def admin_unlock():
    if not ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Admin token has not been configured."}), 503
    data = request.get_json(silent=True) or {}
    submitted_token = str(data.get("token", ""))
    import hmac
    if not hmac.compare_digest(submitted_token, ADMIN_TOKEN):
        session.pop("adami_admin", None)
        return jsonify({"ok": False, "error": "Incorrect admin token."}), 401
    session["adami_admin"] = True
    return jsonify({"ok": True, "message": "Admin access granted."})

# ==========================================
# TEST VOICE ENDPOINT
# ==========================================

@app.route("/api/cartoon/test-voice", methods=["POST"])
def test_voice():
    data = request.get_json(force=True) or {}
    script = data.get("script") or "My people, una don hear? Amara don come back."
    tmpdir = Path(tempfile.mkdtemp())
    try:
        voice_path = tmpdir / "test.mp3"
        result = generate_naija_voice(script, voice_path)
        if not result:
            return {"ok": False, "stage": "elevenlabs_failed", "fix": "Check ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID in Render"}, 500
        return {"ok": True, "duration": round(get_media_duration(result), 2), "size": Path(result).stat().st_size, "message": "Voice works"}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ==========================================
# REAL AI GENERATION - SINGLE MP4 FOR ADMIN + PUBLIC WITH VOICE
# ==========================================

@app.route("/api/cartoon/generate", methods=["POST"])
def generate_cartoon():
    if not RUNWAY_API_KEY:
        return jsonify({"error": "Runway API key is missing. Add RUNWAY_API_KEY in Hugging Face Secrets."}), 503
    data = request.get_json(silent=True) or {}
    try:
        duration = int(data.get("duration", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid video duration."}), 400
    is_admin = session.get("adami_admin") is True
    if is_admin:
        if duration not in (60, 120, 180, 240):
            return jsonify({"error": "Admin videos must be 60, 120, 180 or 240 seconds (1, 2, 3 or 4 minutes)."}), 400
    else:
        if duration != PUBLIC_VIDEO_SECONDS:
            return jsonify({"error": "Public videos are limited to 10 seconds."}), 403
        allowed, remaining = check_public_limit()
        if not allowed:
            hours = remaining // 3600
            return jsonify({"error": f"Your free video has already been used. Please try again in approximately {hours} hours.", "remaining_seconds": remaining}), 429
    try:
        image_uri = validate_image(data.get("image"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    prompt = build_prompt(data)
    script_text = str(data.get("script", "")).strip()
    try:
        if is_admin:
            num_clips = duration // 10
        else:
            num_clips = 1
        video_urls = []
        task_ids = []
        for clip_index in range(num_clips):
            clip_prompt = prompt
            if clip_index > 0:
                clip_prompt = (prompt + f" Continue the story, part {clip_index + 1} of {num_clips}.")[:950]
            task_id = create_runway_task(image_uri, clip_prompt, 10)
            task_ids.append(task_id)
            video_url = wait_for_runway_video(task_id)
            video_urls.append(video_url)
        if not is_admin:
            final_filename = f"adami_public_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
            final_path = DATA_DIRECTORY / final_filename
            try:
                concat_and_add_voice(video_urls, script_text, final_path, duration)
                download_url = f"/api/cartoon/download/{final_filename}"
            except Exception:
                app.logger.exception("Public voice mux failed, falling back to raw")
                download_url = video_urls[0]
            record_public_generation()
            return jsonify({
                "success": True,
                "videoUrl": download_url,
                "video_url": download_url,
                "downloadUrl": download_url,
                "videoUrls": video_urls,
                "video_urls": video_urls,
                "taskId": task_ids[0] if task_ids else None,
                "taskIds": task_ids,
                "duration": duration,
                "clips": num_clips,
                "model": RUNWAY_MODEL,
                "is_single_mp4": True,
                "message": "Your AI video is ready. 1 clip generated."
            }), 200
        final_filename = f"adami_admin_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
        final_path = DATA_DIRECTORY / final_filename
        concat_and_add_voice(video_urls, script_text, final_path, duration)
        download_url = f"/api/cartoon/download/{final_filename}"
        return jsonify({
            "success": True,
            "videoUrl": download_url,
            "video_url": download_url,
            "downloadUrl": download_url,
            "videoUrls": video_urls,
            "video_urls": video_urls,
            "taskId": task_ids[0] if task_ids else None,
            "taskIds": task_ids,
            "duration": duration,
            "clips": num_clips,
            "model": RUNWAY_MODEL,
            "is_single_mp4": True,
            "has_voice": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID),
            "message": f"Your single {duration}s MP4 is ready with Naija voice. {num_clips} clips joined."
        }), 200
    except requests.Timeout:
        app.logger.exception("Runway request timed out")
        return jsonify({"error": "Runway timed out. Please try again."}), 504
    except Exception as error:
        app.logger.exception("Cartoon generation error")
        return jsonify({"error": str(error)}), 500

@app.route("/api/cartoon/download/<filename>", methods=["GET"])
def download_video(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid file."}), 400
    file_path = DATA_DIRECTORY / filename
    if not file_path.exists():
        return jsonify({"error": "Video not found."}), 404
    return send_file(str(file_path), mimetype="video/mp4", as_attachment=False, download_name=filename)

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    if not data.get("image"):
        return jsonify({"error": "This route now requires an uploaded image. Use /api/cartoon/generate."}), 400
    return generate_cartoon()

# ==========================================
# ADAMI AI CODE BUILDER
# ==========================================

CODE_USAGE_FILE = DATA_DIRECTORY / "adami_code_usage.json"
code_usage_lock = threading.Lock()
CODE_MAX_BUILDS_PER_DAY = 5
CODE_MAX_FILES = 30
CODE_MAX_PROJECT_SIZE = 100 * 1024
CODE_BUILDER_PRICING = {
    "starter": {"price": 2000, "included_fixes": 2},
    "business": {"price": 5000, "included_fixes": 5},
    "professional": {"price": 10000, "included_fixes": 10},
    "enterprise": {"price": 25000, "included_fixes": 20}
}
CODE_FIX_PRICING = {"small": 500, "medium": 1000, "major": 2000}
CODE_FIX_PACKAGES = {"fix_3": {"price": 1500, "credits": 3}, "fix_5": {"price": 2500, "credits": 5}}

def load_code_usage():
    try:
        if CODE_USAGE_FILE.exists():
            with open(CODE_USAGE_FILE, "r") as file:
                return json.load(file)
    except Exception:
        app.logger.exception("Could not read Code Builder usage")
    return {}

def save_code_usage(data):
    temporary_file = CODE_USAGE_FILE.with_suffix(".tmp")
    with open(temporary_file, "w") as file:
        json.dump(data, file)
    temporary_file.replace(CODE_USAGE_FILE)

@app.route("/api/code-builder/build", methods=["POST"])
def code_builder_build():
    if not OPENAI_API_KEY:
        return jsonify({"success": False, "error": "AI Code Builder is not configured yet. Please contact the administrator."}), 503
    data = request.get_json(silent=True) or {}
    project_name = str(data.get("projectName", "My ADAMI Project")).strip()[:80]
    prompt = str(data.get("prompt", "")).strip()
    language = str(data.get("language", "html")).lower()
    allowed_languages = ("html", "python", "javascript", "typescript", "react", "nextjs")
    if language not in allowed_languages:
        return jsonify({"success": False, "error": "Unsupported programming language."}), 400
    if len(prompt) < 10:
        return jsonify({"success": False, "error": "Please describe what you want to build."}), 400
    if len(prompt) > 5000:
        return jsonify({"success": False, "error": "Your project description is too long."}), 400
    identifier = get_user_identifier()
    today = time.strftime("%Y-%m-%d")
    with code_usage_lock:
        usage = load_code_usage()
        user_usage = usage.get(identifier, {})
        if user_usage.get("date") != today:
            user_usage = {"date": today, "count": 0}
        if int(user_usage.get("count", 0)) >= CODE_MAX_BUILDS_PER_DAY:
            return jsonify({"success": False, "error": "You have reached your limit of 5 AI builds today. Please try again tomorrow."}), 429
    instructions = """
You are ADAMI AI Code Builder, a professional software engineer.
Build complete, useful software projects based on the user's request.
Return JSON containing:
- summary: a short explanation of the project
- files: an array of files
- each file must contain path and content
RULES:
1. Generate working source code, not explanations instead of code.
2. Include all essential files.
3. Use clean, organized project folders.
4. Never include API keys, passwords or private secrets.
5. Never create malware, credential theft tools or harmful software.
6. Never create code intended to attack other systems.
7. Do not include absolute file paths.
8. Keep files relevant to the requested project.
9. Do not claim that code has been tested or executed.
10. Return valid JSON matching the required structure.
"""
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}
        },
        "required": ["summary", "files"],
        "additionalProperties": False
    }
    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": ("Project name: " + project_name + "\nProgramming language: " + language + "\nUser requirements: " + prompt),
        "text": {"format": {"type": "json_schema", "name": "adami_project", "strict": True, "schema": schema}},
        "max_output_tokens": 12000
    }
    try:
        response = requests.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=120)
        if response.status_code != 200:
            app.logger.error("OpenAI Code Builder error: %s", response.text[:1000])
            return jsonify({"success": False, "error": "The AI builder could not complete your request. Please try again."}), 502
        result = response.json()
        output_text = ""
        for output_item in result.get("output", []):
            if output_item.get("type") == "message":
                for content_item in output_item.get("content", []):
                    if content_item.get("type") == "output_text":
                        output_text += content_item.get("text", "")
        if not output_text:
            return jsonify({"success": False, "error": "The AI returned an empty project. Please try again."}), 502
        project = json.loads(output_text)
        files = project.get("files", [])
        if not isinstance(files, list) or not files:
            return jsonify({"success": False, "error": "The AI did not generate any project files."}), 502
        if len(files) > CODE_MAX_FILES:
            return jsonify({"success": False, "error": "The generated project exceeds the 30-file limit."}), 400
        safe_files = []
        total_size = 0
        used_paths = set()
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).replace("\\", "/").strip()
            content = item.get("content", "")
            if not isinstance(content, str):
                continue
            if (not path or path.startswith("/") or ".." in path.split("/") or ":" in path or "\x00" in path):
                continue
            if len(path) > 180:
                continue
            if path in used_paths:
                continue
            used_paths.add(path)
            file_size = len(content.encode("utf-8"))
            total_size += file_size
            if total_size > CODE_MAX_PROJECT_SIZE:
                return jsonify({"success": False, "error": "The generated project exceeds the 100 KB code limit."}), 400
            safe_files.append({"path": path, "content": content})
        if not safe_files:
            return jsonify({"success": False, "error": "No valid project files were generated."}), 502
        with code_usage_lock:
            usage = load_code_usage()
            user_usage = usage.get(identifier, {})
            if user_usage.get("date") != today:
                user_usage = {"date": today, "count": 0}
            user_usage["count"] = int(user_usage.get("count", 0)) + 1
            usage[identifier] = user_usage
            save_code_usage(usage)
        return jsonify({"success": True, "projectName": project_name, "summary": str(project.get("summary", ""))[:2000], "files": safe_files, "fileCount": len(safe_files), "remainingBuilds": max(0, CODE_MAX_BUILDS_PER_DAY - int(user_usage["count"])), "message": "Your project files have been generated successfully."}), 200
    except requests.Timeout:
        return jsonify({"success": False, "error": "The AI took too long to respond. Please try again."}), 504
    except Exception:
        app.logger.exception("ADAMI Code Builder failed")
        return jsonify({"success": False, "error": "Something went wrong while building your project."}), 500

@app.route("/health")
def health():
    return jsonify({"status": "online", "app": "ADAMI AI Studio", "runway_configured": bool(RUNWAY_API_KEY), "admin_configured": bool(ADMIN_TOKEN), "elevenlabs_configured": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID), "generation_endpoint": "/api/cartoon/generate"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))