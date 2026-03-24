# clean base image containing only comfyui, comfy-cli and comfyui-manager
FROM runpod/worker-comfyui:5.5.1-base

# install custom nodes into comfyui (first node with --mode remote to fetch updated cache)
# The workflow lists only unknown-registry custom nodes without repository information (aux_id), so they could not be resolved automatically.
# Could not resolve the following custom nodes (no registry ID or GitHub repo provided):
# - CheckpointLoaderSimple
# - LoraLoader
# - CLIPTextEncode
# - CLIPTextEncode
# - EmptyLatentImage
# - ADE_AnimateDiffLoaderGen1
# - KSampler
# - VAEDecode
# - VHS_VideoCombine
# If you have registry IDs (cnrId) for any of these, or GitHub repos, I can add comfy node install or git clone commands.

# download models into comfyui
RUN comfy model download --url https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors --relative-path models/checkpoints --filename v1-5-pruned-emaonly.safetensors
RUN comfy model download --url https://huggingface.co/latent-consistency/lcm-lora-sdv1-5/resolve/main/pytorch_lora_weights.safetensors --relative-path models/loras --filename lcm-lora-sdv1-5.safetensors
RUN comfy model download --url https://huggingface.co/guoyww/animatediff/resolve/main/mm_sd_v15_v2.ckpt --relative-path models/animatediff_models --filename mm_sd_v15_v2.ckpt
# COPY input/ /comfyui/input/
