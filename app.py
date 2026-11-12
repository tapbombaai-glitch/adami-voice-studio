import os
import re
import json
import time
import base64
import logging
import threading
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

# Use persistent storage at /data when available.
DATA_DIRECTORY = Path(
    os.environ.get("ADAMI_DATA_DIR", "/data")
)

try:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIRECTORY = Path(".")

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

    # This is an IP-based public limit.
    # Configure trusted proxy handling carefully in production.

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

    # Use the correct image MIME type.
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

    # --------------------------------------
    # Duration and admin authorization
    # --------------------------------------

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

    # --------------------------------------
    # Validate uploaded image
    # --------------------------------------

    try:

        image_uri = validate_image(
            data.get("image")
        )

    except ValueError as error:

        return jsonify({
            "error": str(error)
        }), 400

    # --------------------------------------
    # Build prompt
    # --------------------------------------

    prompt = build_prompt(data)

    # --------------------------------------
    # Generate video
    # --------------------------------------

    try:

        # Runway image-to-video supports
        # individual clips between 2 and 10 seconds.
        #
        # 60/120-second multi-clip assembly
        # requires a separate stitching workflow.

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

        # Only consume the public allowance
        # after a successful video generation.
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

    # Preserve the old route.
    # It now uses the same real video generator.

    if not data.get("image"):

        return jsonify({
            "error": (
                "This route now requires an uploaded image. "
                "Use /api/cartoon/generate."
            )
        }), 400

    return generate_cartoon()


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
