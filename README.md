# runpod_comfyui_animatelcm_portrait_high_quality.workflow.json

Dockerized ComfyUI workflow: runpod_comfyui_animatelcm_portrait_high_quality.workflow.json

## Contents

- `Dockerfile` - Docker container configuration for running this ComfyUI workflow
- `example-request.json` - Example API request payload for testing

## Usage

```bash
# Build the Docker image
docker build -t runpod_comfyui_animatelcm_portrait_high_quality.workflow.json .

# Run the container
docker run -p 8188:8188 runpod_comfyui_animatelcm_portrait_high_quality.workflow.json
```

## Included Dependencies

- `ComfyUI-AnimateDiff-Evolved` is installed for `ADE_AnimateDiffLoaderGen1`
- `ComfyUI-VideoHelperSuite` is installed for `VHS_VideoCombine`
- `ffmpeg` is installed for video export support
- `boto3` is installed so the custom handler can upload final video artifacts to S3
- The image downloads the SD 1.5 checkpoint, LCM LoRA, and the AnimateDiff motion model used by the example workflow

## API Request Example

See `example-request.json` for a ready-to-use Runpod request payload containing a valid ComfyUI API graph.

This image also accepts the legacy `clipflow` request shape with `input.type = "video"` and fields like `prompt`, `frames`, `fps`, `width`, `height`, and `guidance_scale`. The handler builds the ComfyUI workflow internally so you do not need to change `2-submit-video-runpod.py` first.

## RunPod Serverless Test

Use the example payload with your deployed endpoint:

```bash
curl -X POST \
	-H "Authorization: Bearer <runpod_api_key>" \
	-H "Content-Type: application/json" \
	--data @example-request.json \
	https://api.runpod.ai/v2/<endpoint_id>/runsync
```

If the worker is configured with default output handling, the response will contain generated files under `output.images` as base64 data or S3 URLs.

With the custom handler in this repo, successful video jobs return the final `VHS_VideoCombine` artifact under fields such as `output.video_url`, `output.expected_video_url`, `output.artifact_url`, and `output.videos`.

## S3 Upload Setup

The custom handler uploads final video outputs to S3 when these environment variables are set on the Runpod endpoint:

- `BUCKET_ENDPOINT_URL`
- `BUCKET_ACCESS_KEY_ID`
- `BUCKET_SECRET_ACCESS_KEY`

An example file is included in `runpod-s3.env.example`.

Example values:

```env
BUCKET_ENDPOINT_URL=https://your-bucket-name.s3.your-region.amazonaws.com
BUCKET_ACCESS_KEY_ID=AKIA_REPLACE_ME
BUCKET_SECRET_ACCESS_KEY=REPLACE_ME
```

Once these are set, successful jobs will return presigned S3 URLs for the final video output. Without them, the handler falls back to local file paths inside the worker container, which is useful only for local debugging.

## Notes

- This repo no longer relies on the stock `worker-comfyui` handler. `/handler.py` is replaced during image build so the endpoint can accept the existing `clipflow` payload and upload final video outputs.
- `example-request.json` is configured for MP4 output with `video/h264-mp4` because your downstream fetcher expects `.mp4` scene clips.
- The `SaveImage` node remains in the example workflow for debugging and optional frame inspection, but the primary artifact for automation is the `VHS_VideoCombine` video output.

Generated from a ComfyUI workflow and adjusted for Runpod Serverless deployment.
