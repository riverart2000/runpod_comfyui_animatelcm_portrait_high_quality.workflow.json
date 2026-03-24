import base64
import json
import logging
import os
import random
import socket
import tempfile
import time
import traceback
import urllib.parse
import uuid
from io import BytesIO
from typing import Any

import requests
import runpod
import websocket
from runpod.serverless.utils import upload_file_to_bucket, upload_in_memory_object

from network_volume import (
    is_network_volume_debug_enabled,
    run_network_volume_diagnostics,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COMFY_API_AVAILABLE_INTERVAL_MS = int(
    os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", 50)
)
COMFY_API_AVAILABLE_MAX_RETRIES = int(
    os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", 0)
)
COMFY_API_FALLBACK_MAX_RETRIES = 500
COMFY_PID_FILE = "/tmp/comfyui.pid"
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))
COMFY_HOST = "127.0.0.1:8188"

DEFAULT_NEGATIVE_PROMPT = (
    "bright tones, overexposed, static framing, blurry details, low resolution, worst quality, "
    "low quality, jpeg artifacts, oversmoothed skin, plastic skin, distorted face, deformed eyes, "
    "bad anatomy, warped hands, fused fingers, duplicate subjects, extra limbs, ghosting, jitter, "
    "flicker, subtitles, text overlays, watermark, logo, paintings, illustrations, still picture"
)
DEFAULT_CHECKPOINT = "v1-5-pruned-emaonly.safetensors"
DEFAULT_LORA = "lcm-lora-sdv1-5.safetensors"
DEFAULT_MOTION_MODEL = "mm_sd_v15_v2.ckpt"
DEFAULT_SAMPLER = "lcm"
DEFAULT_SCHEDULER = "normal"
DEFAULT_STEPS = 20
DEFAULT_CFG = 3.0
DEFAULT_FRAMES = 16
DEFAULT_FPS = 12
DEFAULT_WIDTH = 768
DEFAULT_HEIGHT = 1344
DEFAULT_VIDEO_FORMAT = "video/h264-mp4"
DEFAULT_FILENAME_PREFIX = "runpod-animate-lcm"


def _comfy_server_status() -> dict[str, Any]:
    try:
        response = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {"reachable": response.status_code == 200, "status_code": response.status_code}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(ws_url: str, max_attempts: int, delay_s: int, initial_error: Exception):
    print(
        f"worker-comfyui-custom - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error = initial_error
    for attempt in range(max_attempts):
        server_status = _comfy_server_status()
        if not server_status["reachable"]:
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print("worker-comfyui-custom - Websocket reconnected successfully.")
            return new_ws
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconnect_error:
            last_reconnect_error = reconnect_error
            print(
                f"worker-comfyui-custom - Reconnect attempt {attempt + 1}/{max_attempts} failed: {reconnect_error}"
            )
            if attempt < max_attempts - 1:
                time.sleep(delay_s)

    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )


def _get_comfyui_pid():
    try:
        with open(COMFY_PID_FILE, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_comfyui_process_alive():
    pid = _get_comfyui_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def check_server(url: str, retries: int = 0, delay: int = 50) -> bool:
    print(f"worker-comfyui-custom - Checking API server at {url}...")
    delay = max(1, delay)
    log_every = max(1, int(10_000 / delay))
    attempt = 0

    while True:
        process_status = _is_comfyui_process_alive()
        if process_status is False:
            print("worker-comfyui-custom - ComfyUI process exited before API became reachable.")
            return False

        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print("worker-comfyui-custom - API is reachable")
                return True
        except requests.Timeout:
            pass
        except requests.RequestException:
            pass

        attempt += 1
        fallback = retries if retries > 0 else COMFY_API_FALLBACK_MAX_RETRIES
        if process_status is None and attempt >= fallback:
            print(
                f"worker-comfyui-custom - Failed to connect to server at {url} after {fallback} attempts."
            )
            return False

        if attempt % log_every == 0:
            elapsed_s = (attempt * delay) / 1000
            print(
                f"worker-comfyui-custom - Still waiting for API server... ({elapsed_s:.0f}s elapsed, attempt {attempt})"
            )

        time.sleep(delay / 1000)


def upload_images(images: list[dict[str, str]] | None) -> dict[str, Any]:
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []
    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]
            base64_data = image_data_uri.split(",", 1)[1] if "," in image_data_uri else image_data_uri
            blob = base64.b64decode(base64_data)
            files = {
                "image": (name, BytesIO(blob), "image/png"),
                "overwrite": (None, "true"),
            }
            response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files, timeout=30)
            response.raise_for_status()
            responses.append(f"Successfully uploaded {name}")
        except Exception as exc:
            upload_errors.append(f"Error uploading {image.get('name', 'unknown')}: {exc}")

    if upload_errors:
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }

    return {"status": "success", "message": "All images uploaded successfully", "details": responses}


def get_available_models() -> dict[str, Any]:
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info = response.json()
        available_models = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )
        return available_models
    except Exception as exc:
        print(f"worker-comfyui-custom - Warning: Could not fetch available models: {exc}")
        return {}


def queue_workflow(workflow: dict[str, Any], client_id: str, comfy_org_api_key: str | None = None):
    payload = {"prompt": workflow, "client_id": client_id}
    key_from_env = os.environ.get("COMFY_ORG_API_KEY")
    effective_key = comfy_org_api_key if comfy_org_api_key else key_from_env
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}

    response = requests.post(
        f"http://{COMFY_HOST}/prompt",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    if response.status_code == 400:
        try:
            error_data = response.json()
            error_message = "Workflow validation failed"
            error_details = []
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                else:
                    error_message = str(error_info)
            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(f"Node {node_id} ({error_type}): {error_msg}")
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")
            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(f"• {detail}" for detail in error_details)
                if any("not in list" in detail and "ckpt_name" in detail for detail in error_details):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += (
                            "\n\nAvailable checkpoint models: "
                            + ", ".join(available_models["checkpoints"])
                        )
                raise ValueError(detailed_message)
            raise ValueError(f"{error_message}. Raw response: {response.text}")
        except (json.JSONDecodeError, KeyError) as exc:
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            ) from exc

    response.raise_for_status()
    return response.json()


def get_history(prompt_id: str):
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_image_data(filename: str, subfolder: str, image_type: str):
    data = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url_values = urllib.parse.urlencode(data)
    response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
    response.raise_for_status()
    return response.content


def get_video_data(video_info: dict[str, Any]) -> bytes:
    query = {
        "filename": video_info.get("filename", ""),
        "subfolder": video_info.get("subfolder", ""),
        "type": video_info.get("type", "output"),
        "format": video_info.get("format", DEFAULT_VIDEO_FORMAT),
    }
    url_values = urllib.parse.urlencode(query)
    response = requests.get(f"http://{COMFY_HOST}/viewvideo?{url_values}", timeout=300)
    response.raise_for_status()
    return response.content


def normalize_legacy_input(job_input: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    workflow = job_input.get("workflow")
    if workflow is not None:
        images = job_input.get("images")
        if images is not None and (
            not isinstance(images, list)
            or not all("name" in image and "image" in image for image in images)
        ):
            raise ValueError("'images' must be a list of objects with 'name' and 'image' keys")
        return {
            "workflow": workflow,
            "images": images,
            "comfy_org_api_key": job_input.get("comfy_org_api_key"),
            "legacy_video": False,
            "response_format": job_input.get("response_format"),
        }, None

    request_type = str(job_input.get("type") or "").strip().lower()
    if request_type != "video":
        raise ValueError("Missing 'workflow' parameter or unsupported input.type. Expected workflow or type='video'.")

    return {
        "workflow": build_legacy_workflow(job_input),
        "images": job_input.get("images"),
        "comfy_org_api_key": job_input.get("comfy_org_api_key"),
        "legacy_video": True,
        "response_format": job_input.get("response_format"),
        "video_prompt": str(job_input.get("video_prompt") or job_input.get("prompt") or "").strip(),
        "negative_prompt": str(job_input.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT).strip(),
        "frames": int(job_input.get("frames") or DEFAULT_FRAMES),
        "fps": int(job_input.get("fps") or DEFAULT_FPS),
        "width": int(job_input.get("output_width") or job_input.get("width") or DEFAULT_WIDTH),
        "height": int(job_input.get("output_height") or job_input.get("height") or DEFAULT_HEIGHT),
    }, None


def build_legacy_workflow(job_input: dict[str, Any]) -> dict[str, Any]:
    prompt = str(job_input.get("video_prompt") or job_input.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Legacy video input requires 'prompt' or 'video_prompt'.")

    negative_prompt = str(job_input.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT).strip()
    width = int(job_input.get("output_width") or job_input.get("width") or DEFAULT_WIDTH)
    height = int(job_input.get("output_height") or job_input.get("height") or DEFAULT_HEIGHT)
    frames = max(1, int(job_input.get("frames") or DEFAULT_FRAMES))
    fps = max(1, int(job_input.get("fps") or DEFAULT_FPS))
    steps = max(1, int(job_input.get("steps") or DEFAULT_STEPS))
    cfg = float(job_input.get("guidance_scale") or job_input.get("cfg") or DEFAULT_CFG)
    seed = int(job_input.get("seed") or random.randint(1, 2**31 - 1))
    filename_prefix = str(job_input.get("filename_prefix") or DEFAULT_FILENAME_PREFIX).strip() or DEFAULT_FILENAME_PREFIX
    video_format = str(job_input.get("format") or job_input.get("video_format") or DEFAULT_VIDEO_FORMAT).strip()

    return {
        "1": {
            "inputs": {"ckpt_name": DEFAULT_CHECKPOINT},
            "class_type": "CheckpointLoaderSimple",
            "_meta": {"title": "CheckpointLoaderSimple"},
        },
        "2": {
            "inputs": {
                "model": ["1", 0],
                "clip": ["1", 1],
                "lora_name": DEFAULT_LORA,
                "strength_model": 0.9,
                "strength_clip": 1,
            },
            "class_type": "LoraLoader",
            "_meta": {"title": "LoraLoader"},
        },
        "3": {
            "inputs": {"text": prompt, "clip": ["2", 1]},
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "CLIPTextEncode"},
        },
        "4": {
            "inputs": {"text": negative_prompt, "clip": ["2", 1]},
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "CLIPTextEncode"},
        },
        "5": {
            "inputs": {"width": width, "height": height, "batch_size": frames},
            "class_type": "EmptyLatentImage",
            "_meta": {"title": "EmptyLatentImage"},
        },
        "6": {
            "inputs": {
                "model": ["2", 0],
                "model_name": DEFAULT_MOTION_MODEL,
                "beta_schedule": "autoselect",
            },
            "class_type": "ADE_AnimateDiffLoaderGen1",
            "_meta": {"title": "ADE_AnimateDiffLoaderGen1"},
        },
        "7": {
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": DEFAULT_SAMPLER,
                "scheduler": DEFAULT_SCHEDULER,
                "denoise": 1,
                "model": ["6", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["5", 0],
            },
            "class_type": "KSampler",
            "_meta": {"title": "KSampler"},
        },
        "8": {
            "inputs": {"samples": ["7", 0], "vae": ["1", 2]},
            "class_type": "VAEDecode",
            "_meta": {"title": "VAEDecode"},
        },
        "9": {
            "inputs": {
                "images": ["8", 0],
                "frame_rate": fps,
                "loop_count": 0,
                "filename_prefix": filename_prefix,
                "format": video_format,
                "pingpong": False,
                "save_output": True,
            },
            "class_type": "VHS_VideoCombine",
            "_meta": {"title": "VHS_VideoCombine"},
        },
        "10": {
            "inputs": {"filename_prefix": f"{filename_prefix}-frame", "images": ["8", 0]},
            "class_type": "SaveImage",
            "_meta": {"title": "SaveImage"},
        },
    }


def guess_content_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".mp4"):
        return "video/mp4"
    if lowered.endswith(".webm"):
        return "video/webm"
    if lowered.endswith(".mkv"):
        return "video/x-matroska"
    if lowered.endswith(".gif"):
        return "image/gif"
    if lowered.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


def upload_artifact_from_bytes(job_id: str, file_name: str, payload: bytes) -> tuple[str, str]:
    url = upload_in_memory_object(file_name=file_name, file_data=payload, prefix=job_id)
    output_type = "s3_url" if os.environ.get("BUCKET_ENDPOINT_URL") else "local_path"
    return url, output_type


def upload_artifact_from_file(job_id: str, file_name: str, file_path: str) -> tuple[str, str]:
    url = upload_file_to_bucket(
        file_name=file_name,
        file_location=file_path,
        prefix=job_id,
        extra_args={"ContentType": guess_content_type(file_name)},
    )
    output_type = "s3_url" if os.environ.get("BUCKET_ENDPOINT_URL") else "local_path"
    return url, output_type


def process_output_images(job_id: str, node_id: str, node_output: dict[str, Any], output_data: list[dict[str, Any]], errors: list[str]):
    if "images" not in node_output:
        return
    print(f"worker-comfyui-custom - Node {node_id} contains {len(node_output['images'])} image(s)")
    for image_info in node_output["images"]:
        filename = image_info.get("filename")
        subfolder = image_info.get("subfolder", "")
        image_type = image_info.get("type")
        if image_type == "temp":
            continue
        if not filename:
            errors.append(f"Skipping image in node {node_id} due to missing filename")
            continue
        try:
            image_bytes = get_image_data(filename, subfolder, image_type)
            if os.environ.get("BUCKET_ENDPOINT_URL"):
                with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1] or ".png", delete=False) as temp_file:
                    temp_file.write(image_bytes)
                    temp_path = temp_file.name
                try:
                    s3_url, output_type = upload_artifact_from_file(job_id, filename, temp_path)
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                output_data.append({"filename": filename, "type": output_type, "data": s3_url})
            else:
                output_data.append(
                    {
                        "filename": filename,
                        "type": "base64",
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    }
                )
        except Exception as exc:
            errors.append(f"Failed to process image output {filename}: {exc}")


def process_output_videos(job_id: str, node_id: str, node_output: dict[str, Any], video_outputs: list[dict[str, Any]], errors: list[str]):
    video_entries = []
    for key in ("gifs", "videos"):
        value = node_output.get(key)
        if isinstance(value, list):
            video_entries.extend(value)

    for video_info in video_entries:
        filename = str(video_info.get("filename") or "").strip()
        if not filename:
            errors.append(f"Skipping video output in node {node_id} due to missing filename")
            continue
        fullpath = str(video_info.get("fullpath") or "").strip()
        try:
            if fullpath and os.path.exists(fullpath):
                url, output_type = upload_artifact_from_file(job_id, filename, fullpath)
            else:
                video_bytes = get_video_data(video_info)
                url, output_type = upload_artifact_from_bytes(job_id, filename, video_bytes)
            video_outputs.append(
                {
                    "filename": filename,
                    "type": output_type,
                    "data": url,
                    "format": video_info.get("format"),
                    "frame_rate": video_info.get("frame_rate"),
                    "subfolder": video_info.get("subfolder", ""),
                }
            )
        except Exception as exc:
            errors.append(f"Failed to process video output {filename}: {exc}")


def handler(job):
    if is_network_volume_debug_enabled():
        run_network_volume_diagnostics()

    job_input = job["input"]
    job_id = job["id"]

    if job_input is None:
        return {"error": "Please provide input"}
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return {"error": "Invalid JSON format in input"}

    try:
        validated_data, _ = normalize_legacy_input(job_input)
    except ValueError as exc:
        return {"error": str(exc)}

    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")

    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {"error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."}

    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            return {
                "error": "Failed to upload one or more input images",
                "details": upload_result["details"],
            }

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    image_outputs = []
    video_outputs = []
    errors = []

    try:
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)

        queued_workflow = queue_workflow(
            workflow,
            client_id,
            comfy_org_api_key=validated_data.get("comfy_org_api_key"),
        )
        prompt_id = queued_workflow.get("prompt_id")
        if not prompt_id:
            raise ValueError(f"Missing 'prompt_id' in queue response: {queued_workflow}")

        execution_done = False
        while True:
            try:
                out = ws.recv()
                if not isinstance(out, str):
                    continue
                message = json.loads(out)
                if message.get("type") == "executing":
                    data = message.get("data", {})
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        execution_done = True
                        break
                elif message.get("type") == "execution_error":
                    data = message.get("data", {})
                    if data.get("prompt_id") == prompt_id:
                        error_details = (
                            f"Node Type: {data.get('node_type')}, Node ID: {data.get('node_id')}, "
                            f"Message: {data.get('exception_message')}"
                        )
                        errors.append(f"Workflow execution error: {error_details}")
                        break
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException as closed_error:
                ws = _attempt_websocket_reconnect(
                    ws_url,
                    WEBSOCKET_RECONNECT_ATTEMPTS,
                    WEBSOCKET_RECONNECT_DELAY_S,
                    closed_error,
                )

        if not execution_done and not errors:
            raise ValueError("Workflow monitoring loop exited without completion or error.")

        history = get_history(prompt_id)
        if prompt_id not in history:
            return {"error": f"Prompt ID {prompt_id} not found in history after execution."}

        prompt_history = history.get(prompt_id, {})
        outputs = prompt_history.get("outputs", {})
        for node_id, node_output in outputs.items():
            process_output_images(job_id, node_id, node_output, image_outputs, errors)
            process_output_videos(job_id, node_id, node_output, video_outputs, errors)
            other_keys = [key for key in node_output.keys() if key not in {"images", "gifs", "videos"}]
            if other_keys:
                print(
                    f"worker-comfyui-custom - Node {node_id} produced unhandled output keys: {other_keys}"
                )

    except websocket.WebSocketException as exc:
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {exc}"}
    except requests.RequestException as exc:
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {exc}"}
    except ValueError as exc:
        print(traceback.format_exc())
        return {"error": str(exc)}
    except Exception as exc:
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {exc}"}
    finally:
        if ws and ws.connected:
            ws.close()

    if validated_data.get("legacy_video") and not video_outputs:
        details = errors or ["Workflow completed without a video output from VHS_VideoCombine."]
        return {"error": "Job processing failed", "details": details}

    result = {}
    if image_outputs:
        result["images"] = image_outputs
    if video_outputs:
        result["videos"] = video_outputs
        result["video_url"] = video_outputs[0]["data"]
        result["expected_video_url"] = video_outputs[0]["data"]
        result["artifact_url"] = video_outputs[0]["data"]
        result["filename"] = video_outputs[0]["filename"]
        result["status"] = "success"
    elif not errors:
        result["status"] = "success_no_videos"

    if errors:
        result["errors"] = errors
        if not image_outputs and not video_outputs:
            return {"error": "Job processing failed", "details": errors}

    return result


if __name__ == "__main__":
    print("worker-comfyui-custom - Starting handler...")
    runpod.serverless.start({"handler": handler})