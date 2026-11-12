import os
import re
import json
import time
import base64
import logging
import threading
import uuid
import sqlite3
from pathlib import Path

import requests
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    session
)

# ==========================================
# ADAMI AI VIDEO STUDIO
# ==========================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET"
)

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

# ADAMI AI CODE BUILDER
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-4o-mini"
)

ADMIN_TOKEN = os.environ.get("ADAMI_ADMIN_TOKEN", "")

RUNWAY_VERSION = "2024-11-06"

RUNWAY_MODEL = os.environ.get(
    "RUNWAY_MODEL",
    "gen4.5"
)

RUNWAY_BASE_URL = "https://api.dev.runwayml.com/v1"

# ==========================================
# PUBLIC GENERATION LIMIT
# ==========================================

PUBLIC_VIDEO_SECONDS = 10

PUBLIC_COOLDOWN_SECONDS = 3 * 24 * 60 * 60

# ==========================================
# ADAMI PERMANENT STORAGE
# ==========================================

DATA_DIRECTORY = Path(
    os.environ.get("ADAMI_DATA_DIR", "/data")
)

try:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)

    test_file = DATA_DIRECTORY / ".adami_storage_test"

    test_file.write_text("storage_ok", encoding="utf-8")
    test_file.unlink()

    logging.info("ADAMI persistent storage is ready.")

except Exception as error:

    logging.critical(
        "ADAMI persistent storage is unavailable: %s",
        error
    )

    raise RuntimeError(
        "ADAMI cannot start because persistent storage is unavailable."
    ) from error

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

        last_generation = int(
            usage.get(identifier, 0)
        )

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
# IMAGE VALIDATION
# ==========================================

def validate_image(image_data):

    if not isinstance(image_data, str):
        raise ValueError("Please upload a character photo.")

    match = re.match(
        r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$",
        image_data,
        re.IGNORECASE | re.DOTALL
    )

    if not match:
        raise ValueError(
            "Invalid photo. Upload a JPG, PNG or WEBP image."
        )

    image_format = match.group(1).lower()

    encoded_data = match.group(2)

    try:
        decoded = base64.b64decode(
            encoded_data,
            validate=True
        )
    except Exception:
        raise ValueError("The uploaded photo is damaged.")

    if len(decoded) > 5 * 1024 * 1024:
        raise ValueError(
            "Your photo must be 5MB or smaller."
        )

    if len(decoded) < 100:
        raise ValueError("The uploaded photo is too small.")

    if image_format == "jpg":
        image_format = "jpeg"

    return (
        f"data:image/{image_format};base64,{encoded_data}"
    )

# ==========================================
# BUILD AI VIDEO PROMPT
# ==========================================

def build_prompt(data):

    style = str(
        data.get("style", "realistic human cartoon")
    )[:200]

    script = str(
        data.get("script", "")
    )[:2000]

    direction = str(
        data.get("direction", "")
    )[:1500]

    emotion = str(
        data.get("emotion", "Natural")
    )[:100]

    voice = str(
        data.get("voice", "main-naija-pidgin")
    )[:100]

    prompt = f"""
Create a high-quality cinematic animated video
using the uploaded image as the first frame.

CHARACTER STYLE:
{style}

CHARACTER DIRECTION:
{direction}

ACTION AND STORY:
{script}

EMOTION:
{emotion}

VOICE STYLE CONTEXT:
{voice}

IMPORTANT VISUAL INSTRUCTIONS:

Preserve the identity and recognizable appearance
of the person in the reference photograph.

Keep the same face, skin tone, hairstyle and clothing
as consistently as possible.

Use natural body movement, realistic expressions,
natural blinking, believable gestures and cinematic
camera movement.

The scene should feel like a professionally produced
Nigerian or African short film.

Avoid changing the character's identity.
Avoid distorted faces, extra fingers and unnatural
body movements.

Generate visually coherent motion.
"""

    return prompt.strip()

# ==========================================
# CREATE RUNWAY VIDEO TASK
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

    response = requests.post(
        f"{RUNWAY_BASE_URL}/image_to_video",
        headers=runway_headers(),
        json=payload,
        timeout=60
    )

    if response.status_code not in (200, 201):

        try:
            details = response.json()
        except Exception:
            details = response.text[:1000]

        app.logger.error(
            "Runway task creation failed: %s",
            details
        )

        raise RuntimeError(
            f"Runway rejected the request: {details}"
        )

    result = response.json()

    task_id = result.get("id")

    if not task_id:
        raise RuntimeError(
            "Runway did not return a task ID."
        )

    return task_id

# ==========================================
# CHECK RUNWAY TASK
# ==========================================

def wait_for_runway_video(task_id):

    maximum_wait = 360

    start_time = time.time()

    while time.time() - start_time < maximum_wait:

        response = requests.get(
            f"{RUNWAY_BASE_URL}/tasks/{task_id}",
            headers=runway_headers(),
            timeout=30
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Could not check Runway generation status: "
                + response.text[:500]
            )

        result = response.json()

        status = result.get("status", "").upper()

        app.logger.info(
            "Runway task %s status: %s",
            task_id,
            status
        )

        if status == "SUCCEEDED":

            output = result.get("output")

            if isinstance(output, list) and output:

                return output[0]

            if isinstance(output, str):

                return output

            raise RuntimeError(
                "Runway finished but returned no video URL."
            )

        if status in ("FAILED", "CANCELLED", "THREW_EXCEPTION"):

            raise RuntimeError(
                "Runway video generation failed: "
                + str(result.get("failure", result.get("error", status)))
            )

        time.sleep(5)

    raise RuntimeError(
        "Video generation is taking too long. "
        "Please try again."
    )

# ==========================================
# HOMEPAGE
# ==========================================

@app.route("/")
def home():

    return send_from_directory(
        ".",
        "index.html"
    )

# ==========================================
# ADMIN UNLOCK
# ==========================================

@app.route(
    "/api/cartoon/admin/unlock",
    methods=["POST"]
)
def admin_unlock():

    if not ADMIN_TOKEN:

        return jsonify({
            "ok": False,
            "error": "Admin token has not been configured."
        }), 503

    data = request.get_json(silent=True) or {}

    submitted_token = str(
        data.get("token", "")
    )

    import hmac

    if not hmac.compare_digest(
        submitted_token,
        ADMIN_TOKEN
    ):

        session.pop("adami_admin", None)

        return jsonify({
            "ok": False,
            "error": "Incorrect admin token."
        }), 401

    session["adami_admin"] = True

    return jsonify({
        "ok": True,
        "message": "Admin access granted."
    })

# ==========================================
# REAL AI GENERATION ENDPOINT
# ==========================================

@app.route(
    "/api/cartoon/generate",
    methods=["POST"]
)
def generate_cartoon():

    if not RUNWAY_API_KEY:

        return jsonify({
            "error": "Runway API key is missing. Add RUNWAY_API_KEY in Hugging Face Secrets."
        }), 503

    data = request.get_json(silent=True) or {}

    try:
        duration = int(data.get("duration", 10))
    except (TypeError, ValueError):

        return jsonify({
            "error": "Invalid video duration."
        }), 400

    is_admin = session.get("adami_admin") is True

    if is_admin:

        if duration not in (60, 120):

            return jsonify({
                "error": "Admin videos must be 60 or 120 seconds."
            }), 400

    else:

        if duration != PUBLIC_VIDEO_SECONDS:

            return jsonify({
                "error": "Public videos are limited to 10 seconds."
            }), 403

        allowed, remaining = check_public_limit()

        if not allowed:

            hours = remaining // 3600

            return jsonify({
                "error": (
                    "Your free video has already been used. "
                    f"Please try again in approximately {hours} hours."
                ),
                "remaining_seconds": remaining
            }), 429

    try:

        image_uri = validate_image(
            data.get("image")
        )

    except ValueError as error:

        return jsonify({
            "error": str(error)
        }), 400

    prompt = build_prompt(data)

    try:

        if duration != 10:

            return jsonify({
                "error": (
                    "The 60/120-second multi-clip assembly "
                    "is not yet connected. This endpoint currently "
                    "generates a single 10-second Runway clip."
                )
            }), 501

        task_id = create_runway_task(
            image_uri,
            prompt,
            10
        )

        video_url = wait_for_runway_video(
            task_id
        )

        if not is_admin:

            record_public_generation()

        return jsonify({

            "success": True,

            "videoUrl": video_url,

            "video_url": video_url,

            "taskId": task_id,

            "duration": 10,

            "model": RUNWAY_MODEL,

            "message": "Your AI video is ready."

        }), 200

    except requests.Timeout:

        app.logger.exception(
            "Runway request timed out"
        )

        return jsonify({
            "error": "Runway timed out. Please try again."
        }), 504

    except Exception as error:

        app.logger.exception(
            "Cartoon generation error"
        )

        return jsonify({
            "error": str(error)
        }), 500

# ==========================================
# KEEP OLD API ROUTE WORKING
# ==========================================

@app.route(
    "/api/generate",
    methods=["POST"]
)
def generate():

    data = request.get_json(silent=True) or {}

    if not data.get("image"):

        return jsonify({
            "error": (
                "This route now requires an uploaded image. "
                "Use /api/cartoon/generate."
            )
        }), 400

    return generate_cartoon()

# ==========================================
# ADAMI AI CODE BUILDER
# ==========================================

CODE_USAGE_FILE = DATA_DIRECTORY / "adami_code_usage.json"
code_usage_lock = threading.Lock()

CODE_MAX_BUILDS_PER_DAY = 5
CODE_MAX_FILES = 30
CODE_MAX_PROJECT_SIZE = 100 * 1024

# ==========================================
# OFFICIAL ADAMI CODE BUILDER PRICING
# ==========================================

CODE_BUILDER_PRICING = {
    "starter": {
        "price": 2000,
        "included_fixes": 2
    },
    "business": {
        "price": 5000,
        "included_fixes": 5
    },
    "professional": {
        "price": 10000,
        "included_fixes": 10
    },
    "enterprise": {
        "price": 25000,
        "included_fixes": 20
    }
}

CODE_FIX_PRICING = {
    "small": 500,
    "medium": 1000,
    "major": 2000
}

CODE_FIX_PACKAGES = {
    "fix_3": {
        "price": 1500,
        "credits": 3
    },
    "fix_5": {
        "price": 2500,
        "credits": 5
    }
}

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

        return jsonify({
            "success": False,
            "error": "AI Code Builder is not configured yet. Please contact the administrator."
        }), 503

    data = request.get_json(silent=True) or {}

    project_name = str(
        data.get("projectName", "My ADAMI Project")
    ).strip()[:80]

    prompt = str(
        data.get("prompt", "")
    ).strip()

    language = str(
        data.get("language", "html")
    ).lower()

    allowed_languages = (
        "html",
        "python",
        "javascript",
        "typescript",
        "react",
        "nextjs"
    )

    if language not in allowed_languages:

        return jsonify({
            "success": False,
            "error": "Unsupported programming language."
        }), 400

    if len(prompt) < 10:

        return jsonify({
            "success": False,
            "error": "Please describe what you want to build."
        }), 400

    if len(prompt) > 5000:

        return jsonify({
            "success": False,
            "error": "Your project description is too long."
        }), 400

    # --------------------------------------
    # DAILY BUILD LIMIT
    # --------------------------------------

    identifier = get_user_identifier()
    today = time.strftime("%Y-%m-%d")

    with code_usage_lock:

        usage = load_code_usage()

        user_usage = usage.get(identifier, {})

        if user_usage.get("date") != today:

            user_usage = {
                "date": today,
                "count": 0
            }

        if int(user_usage.get("count", 0)) >= CODE_MAX_BUILDS_PER_DAY:

            return jsonify({
                "success": False,
                "error": "You have reached your limit of 5 AI builds today. Please try again tomorrow."
            }), 429

    # --------------------------------------
    # AI PROJECT GENERATION
    # --------------------------------------

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
            "summary": {
                "type": "string"
            },
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string"
                        },
                        "content": {
                            "type": "string"
                        }
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["summary", "files"],
        "additionalProperties": False
    }

    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": (
            "Project name: " + project_name +
            "\nProgramming language: " + language +
            "\nUser requirements: " + prompt
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "adami_project",
                "strict": True,
                "schema": schema
            }
        },
        "max_output_tokens": 12000
    }

    try:

        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=120
        )

        if response.status_code != 200:

            app.logger.error(
                "OpenAI Code Builder error: %s",
                response.text[:1000]
            )

            return jsonify({
                "success": False,
                "error": "The AI builder could not complete your request. Please try again."
            }), 502

        result = response.json()

        output_text = ""

        for output_item in result.get("output", []):

            if output_item.get("type") == "message":

                for content_item in output_item.get("content", []):

                    if content_item.get("type") == "output_text":

                        output_text += content_item.get("text", "")

        if not output_text:

            app.logger.error(
                "OpenAI returned no output text."
            )

            return jsonify({
                "success": False,
                "error": "The AI returned an empty project. Please try again."
            }), 502

        project = json.loads(output_text)

        files = project.get("files", [])

        if not isinstance(files, list) or not files:

            return jsonify({
                "success": False,
                "error": "The AI did not generate any project files."
            }), 502

        if len(files) > CODE_MAX_FILES:

            return jsonify({
                "success": False,
                "error": "The generated project exceeds the 30-file limit."
            }), 400

        # ----------------------------------
        # VALIDATE GENERATED FILES
        # ----------------------------------

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

            if (
                not path
                or path.startswith("/")
                or ".." in path.split("/")
                or ":" in path
                or "\x00" in path
            ):
                continue

            if len(path) > 180:
                continue

            if path in used_paths:
                continue

            used_paths.add(path)

            file_size = len(content.encode("utf-8"))
            total_size += file_size

            if total_size > CODE_MAX_PROJECT_SIZE:

                return jsonify({
                    "success": False,
                    "error": "The generated project exceeds the 100 KB code limit."
                }), 400

            safe_files.append({
                "path": path,
                "content": content
            })

        if not safe_files:

            return jsonify({
                "success": False,
                "error": "No valid project files were generated."
            }), 502

        # ----------------------------------
        # SAVE DAILY USAGE AFTER SUCCESS
        # ----------------------------------

        with code_usage_lock:

            usage = load_code_usage()

            user_usage = usage.get(identifier, {})

            if user_usage.get("date") != today:

                user_usage = {
                    "date": today,
                    "count": 0
                }

            user_usage["count"] = int(
                user_usage.get("count", 0)
            ) + 1

            usage[identifier] = user_usage

            save_code_usage(usage)

        return jsonify({

            "success": True,

            "projectName": project_name,

            "summary": str(
                project.get("summary", "")
            )[:2000],

            "files": safe_files,

            "fileCount": len(safe_files),

            "remainingBuilds": max(
                0,
                CODE_MAX_BUILDS_PER_DAY -
                int(user_usage["count"])
            ),

            "message": "Your project files have been generated successfully."

        }), 200

    except requests.Timeout:

        return jsonify({
            "success": False,
            "error": "The AI took too long to respond. Please try again."
        }), 504

    except Exception:

        app.logger.exception(
            "ADAMI Code Builder failed"
        )

        return jsonify({
            "success": False,
            "error": "Something went wrong while building your project."
        }), 500

# ==========================================
# HEALTH CHECK
# ==========================================

@app.route("/health")
def health():

    return jsonify({

        "status": "online",

        "app": "ADAMI AI Studio",

        "runway_configured": bool(RUNWAY_API_KEY),

        "admin_configured": bool(ADMIN_TOKEN),

        "generation_endpoint": "/api/cartoon/generate"

    })

# ==========================================
# START SERVER
# ==========================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 7860))
    )