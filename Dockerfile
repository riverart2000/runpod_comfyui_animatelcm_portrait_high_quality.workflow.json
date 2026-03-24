# clean base image containing only comfyui, comfy-cli and comfyui-manager
FROM runpod/worker-comfyui:5.5.1-base

# install system dependencies and required custom nodes
RUN apt-get update \
	&& apt-get install -y --no-install-recommends ffmpeg git \
	&& rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3

RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved /comfyui/custom_nodes/ComfyUI-AnimateDiff-Evolved \
	&& if [ -f /comfyui/custom_nodes/ComfyUI-AnimateDiff-Evolved/requirements.txt ]; then pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-AnimateDiff-Evolved/requirements.txt; fi

RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite /comfyui/custom_nodes/ComfyUI-VideoHelperSuite \
	&& if [ -f /comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt ]; then pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt; fi

# download models into comfyui
RUN comfy model download --url https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors --relative-path models/checkpoints --filename v1-5-pruned-emaonly.safetensors
RUN comfy model download --url https://huggingface.co/latent-consistency/lcm-lora-sdv1-5/resolve/main/pytorch_lora_weights.safetensors --relative-path models/loras --filename lcm-lora-sdv1-5.safetensors
RUN comfy model download --url https://huggingface.co/guoyww/animatediff/resolve/main/mm_sd_v15_v2.ckpt --relative-path models/animatediff_models --filename mm_sd_v15_v2.ckpt

COPY handler.py /handler.py
# COPY input/ /comfyui/input/
