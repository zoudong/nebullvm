# Based on https://github.com/NVIDIA/TensorRT/blob/main/demo/Diffusion/models.py
#
#
# SPDX-FileCopyrightText: Copyright (c) 1993-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from typing import Dict, Union, List, Optional, Any, Tuple

from nebullvm.optional_modules.diffusers import (
    DiffusionPipeline,
    UNet2DConditionModel,
    UNet2DOutput,
    AutoencoderKL,
    onnx_graphsurgeon as gs,
)
from nebullvm.optional_modules.onnx import onnx
from nebullvm.optional_modules.tensor_rt import fold_constants
from nebullvm.optional_modules.torch import torch
from nebullvm.optional_modules.huggingface import CLIPTextModel, CLIPTokenizer
from nebullvm.tools.base import Device


@torch.no_grad()
def get_unet_inputs(
    self,
    prompt: Union[str, List[str]] = None,
    height: Optional[int] = None,
    width: Optional[int] = None,
    num_inference_steps: int = 1,
    guidance_scale: float = 7.5,
    negative_prompt: Optional[Union[str, List[str]]] = None,
    num_images_per_prompt: Optional[int] = 1,
    generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    latents: Optional[torch.FloatTensor] = None,
    prompt_embeds: Optional[torch.FloatTensor] = None,
    negative_prompt_embeds: Optional[torch.FloatTensor] = None,
    callback_steps: int = 1,
    cross_attention_kwargs: Optional[Dict[str, Any]] = None,
):
    # 0. Default height and width to unet
    height = height or self.unet.config.sample_size * self.vae_scale_factor
    width = width or self.unet.config.sample_size * self.vae_scale_factor

    # 1. Check inputs. Raise error if not correct
    self.check_inputs(
        prompt,
        height,
        width,
        callback_steps,
        negative_prompt,
        prompt_embeds,
        negative_prompt_embeds,
    )

    # 2. Define call parameters
    if prompt is not None and isinstance(prompt, str):
        batch_size = 1
    elif prompt is not None and isinstance(prompt, list):
        batch_size = len(prompt)
    else:
        batch_size = prompt_embeds.shape[0]

    device = self._execution_device
    do_classifier_free_guidance = guidance_scale > 1.0

    # 3. Encode input prompt
    prompt_embeds = self._encode_prompt(
        prompt,
        device,
        num_images_per_prompt,
        do_classifier_free_guidance,
        negative_prompt,
        prompt_embeds=prompt_embeds,
        negative_prompt_embeds=negative_prompt_embeds,
    )

    # 4. Prepare timesteps
    self.scheduler.set_timesteps(num_inference_steps, device=device)
    timesteps = self.scheduler.timesteps

    # 5. Prepare latent variables
    num_channels_latents = self.unet.in_channels
    latents = self.prepare_latents(
        batch_size * num_images_per_prompt,
        num_channels_latents,
        height,
        width,
        prompt_embeds.dtype,
        device,
        generator,
        latents,
    )

    for i, t in enumerate(timesteps):
        # expand the latents if we are doing classifier free guidance
        latent_model_input = (
            torch.cat([latents] * 2)
            if do_classifier_free_guidance
            else latents
        )
        latent_model_input = self.scheduler.scale_model_input(
            latent_model_input, t
        )

        return latent_model_input, t, prompt_embeds, cross_attention_kwargs


class DiffusionUNetWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, *x, **kwargs):
        return tuple(
            self.model(x[0], x[1], encoder_hidden_states=x[2]).values()
        )


class OptimizedDiffusionWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, *x, **kwargs):
        return UNet2DOutput(
            self.model(
                x[0],
                x[1].reshape((1,)) if x[1].shape == torch.Size([]) else x[1],
                kwargs["encoder_hidden_states"],
            )[0]
        )


def is_diffusion_model_pipe(model):
    return isinstance(model, DiffusionPipeline)


def get_default_dynamic_info(input_shape: List[Tuple[int, ...]]):
    return {
        "inputs": [
            {
                0: {
                    "name": "2B",
                    "min_val": input_shape[0][0],
                    "opt_val": input_shape[0][0],
                    "max_val": input_shape[0][0],
                },
                2: {
                    "name": "H",
                    "min_val": input_shape[0][2],
                    "opt_val": input_shape[0][2],
                    "max_val": input_shape[0][2],
                },
                3: {
                    "name": "W",
                    "min_val": input_shape[0][3],
                    "opt_val": input_shape[0][3],
                    "max_val": input_shape[0][3],
                },
            },
            {},
            {
                0: {
                    "name": "2B",
                    "min_val": input_shape[2][0],
                    "opt_val": input_shape[2][0],
                    "max_val": input_shape[2][0],
                }
            },
        ],
        "outputs": [{0: "2B", 2: "H", 3: "W"}],
    }


def preprocess_diffusers(pipe: DiffusionPipeline) -> torch.nn.Module:
    # Function that wraps the Diffusion UNet model to
    # be compatible with the optimizations performed by nebullvm
    model = DiffusionUNetWrapper(pipe.unet)
    return model


def postprocess_diffusers(
    optimized_model: Any,
    pipe: DiffusionPipeline,
    device: Device,
) -> DiffusionPipeline:
    # Function that puts the optimized Diffusion UNet model back
    # into the Diffusion Pipeline
    final_model = OptimizedDiffusionWrapper(optimized_model)
    final_model.sample_size = pipe.unet.sample_size
    final_model.in_channels = pipe.unet.in_channels
    final_model.device = torch.device(device.to_torch_format())
    final_model.config = pipe.unet.config
    final_model.in_channels = pipe.unet.in_channels
    pipe.unet = final_model
    return pipe


class Optimizer:
    def __init__(self, onnx_graph, verbose=False):
        self.graph = gs.import_onnx(onnx_graph)
        self.verbose = verbose

    def info(self, prefix):
        if self.verbose:
            print(
                f"{prefix} .. {len(self.graph.nodes)} nodes, {len(self.graph.tensors().keys())} tensors, {len(self.graph.inputs)} inputs, {len(self.graph.outputs)} outputs"
            )

    def cleanup(self, return_onnx=False):
        self.graph.cleanup().toposort()
        if return_onnx:
            return gs.export_onnx(self.graph)

    def select_outputs(self, keep, names=None):
        self.graph.outputs = [self.graph.outputs[o] for o in keep]
        if names:
            for i, name in enumerate(names):
                self.graph.outputs[i].name = name

    def fold_constants(self, return_onnx=False):
        onnx_graph = fold_constants(
            gs.export_onnx(self.graph),
            allow_onnxruntime_shape_inference=True,
        )
        self.graph = gs.import_onnx(onnx_graph)
        if return_onnx:
            return onnx_graph

    def infer_shapes(self, return_onnx=False):
        onnx_graph = gs.export_onnx(self.graph)
        if onnx_graph.ByteSize() > 2147483648:
            raise TypeError("ERROR: model size exceeds supported 2GB limit")
        else:
            onnx_graph = onnx.shape_inference.infer_shapes(onnx_graph)

        self.graph = gs.import_onnx(onnx_graph)
        if return_onnx:
            return onnx_graph


def get_path(version, inpaint=False):
    if version == "1.4":
        if inpaint:
            return "runwayml/stable-diffusion-inpainting"
        else:
            return "CompVis/stable-diffusion-v1-4"
    elif version == "1.5":
        if inpaint:
            return "runwayml/stable-diffusion-inpainting"
        else:
            return "runwayml/stable-diffusion-v1-5"
    elif version == "2.0-base":
        if inpaint:
            return "stabilityai/stable-diffusion-2-inpainting"
        else:
            return "stabilityai/stable-diffusion-2-base"
    elif version == "2.0":
        if inpaint:
            return "stabilityai/stable-diffusion-2-inpainting"
        else:
            return "stabilityai/stable-diffusion-2"
    elif version == "2.1":
        return "stabilityai/stable-diffusion-2-1"
    elif version == "2.1-base":
        return "stabilityai/stable-diffusion-2-1-base"
    else:
        raise ValueError(f"Incorrect version {version}")


def get_embedding_dim(version):
    if version in ("1.4", "1.5"):
        return 768
    elif version in ("2.0", "2.0-base", "2.1", "2.1-base"):
        return 1024
    else:
        raise ValueError(f"Incorrect version {version}")


class BaseModel:
    def __init__(
        self,
        hf_token,
        fp16=False,
        device="cuda",
        verbose=False,
        path="",
        max_batch_size=16,
        embedding_dim=768,
        text_maxlen=77,
    ):
        self.name = "SD Model"
        self.hf_token = hf_token
        self.fp16 = fp16
        self.device = device
        self.verbose = verbose
        self.path = path

        self.min_batch = 1
        self.max_batch = max_batch_size
        self.min_image_shape = 256  # min image resolution: 256x256
        self.max_image_shape = 1024  # max image resolution: 1024x1024
        self.min_latent_shape = self.min_image_shape // 8
        self.max_latent_shape = self.max_image_shape // 8

        self.embedding_dim = embedding_dim
        self.text_maxlen = text_maxlen

    def get_model(self):
        pass

    def get_input_names(self):
        pass

    def get_output_names(self):
        pass

    def get_dynamic_axes(self):
        return None

    def get_sample_input(self, batch_size, image_height, image_width):
        pass

    def get_input_profile(
        self, batch_size, image_height, image_width, static_batch, static_shape
    ):
        return None

    def get_shape_dict(self, batch_size, image_height, image_width):
        return None

    def optimize(self, onnx_graph):
        opt = Optimizer(onnx_graph, verbose=self.verbose)
        opt.info(self.name + ": original")
        opt.cleanup()
        opt.info(self.name + ": cleanup")
        opt.fold_constants()
        opt.info(self.name + ": fold constants")
        opt.infer_shapes()
        opt.info(self.name + ": shape inference")
        onnx_opt_graph = opt.cleanup(return_onnx=True)
        opt.info(self.name + ": finished")
        return onnx_opt_graph

    def check_dims(self, batch_size, image_height, image_width):
        assert batch_size >= self.min_batch and batch_size <= self.max_batch
        assert image_height % 8 == 0 or image_width % 8 == 0
        latent_height = image_height // 8
        latent_width = image_width // 8
        assert (
            latent_height >= self.min_latent_shape
            and latent_height <= self.max_latent_shape
        )
        assert (
            latent_width >= self.min_latent_shape
            and latent_width <= self.max_latent_shape
        )
        return (latent_height, latent_width)

    def get_minmax_dims(
        self, batch_size, image_height, image_width, static_batch, static_shape
    ):
        min_batch = batch_size if static_batch else self.min_batch
        max_batch = batch_size if static_batch else self.max_batch
        latent_height = image_height // 8
        latent_width = image_width // 8
        min_image_height = (
            image_height if static_shape else self.min_image_shape
        )
        max_image_height = (
            image_height if static_shape else self.max_image_shape
        )
        min_image_width = image_width if static_shape else self.min_image_shape
        max_image_width = image_width if static_shape else self.max_image_shape
        min_latent_height = (
            latent_height if static_shape else self.min_latent_shape
        )
        max_latent_height = (
            latent_height if static_shape else self.max_latent_shape
        )
        min_latent_width = (
            latent_width if static_shape else self.min_latent_shape
        )
        max_latent_width = (
            latent_width if static_shape else self.max_latent_shape
        )
        return (
            min_batch,
            max_batch,
            min_image_height,
            max_image_height,
            min_image_width,
            max_image_width,
            min_latent_height,
            max_latent_height,
            min_latent_width,
            max_latent_width,
        )


class CLIP(BaseModel):
    def __init__(
        self, hf_token, device, verbose, path, max_batch_size, embedding_dim
    ):
        super(CLIP, self).__init__(
            hf_token,
            device=device,
            verbose=verbose,
            path=path,
            max_batch_size=max_batch_size,
            embedding_dim=embedding_dim,
        )
        self.name = "CLIP"

    def get_model(self):
        return CLIPTextModel.from_pretrained(
            self.path, subfolder="text_encoder", use_auth_token=self.hf_token
        ).to(self.device)

    def get_input_names(self):
        return ["input_ids"]

    def get_output_names(self):
        return ["text_embeddings", "pooler_output"]

    def get_dynamic_axes(self):
        return {"input_ids": {0: "B"}, "text_embeddings": {0: "B"}}

    def get_input_profile(
        self, batch_size, image_height, image_width, static_batch, static_shape
    ):
        self.check_dims(batch_size, image_height, image_width)
        min_batch, max_batch, _, _, _, _, _, _, _, _ = self.get_minmax_dims(
            batch_size, image_height, image_width, static_batch, static_shape
        )
        return {
            "input_ids": [
                (min_batch, self.text_maxlen),
                (batch_size, self.text_maxlen),
                (max_batch, self.text_maxlen),
            ]
        }

    def get_shape_dict(self, batch_size, image_height, image_width):
        self.check_dims(batch_size, image_height, image_width)
        return {
            "input_ids": (batch_size, self.text_maxlen),
            "text_embeddings": (
                batch_size,
                self.text_maxlen,
                self.embedding_dim,
            ),
        }

    def get_sample_input(self, batch_size, image_height, image_width):
        self.check_dims(batch_size, image_height, image_width)
        return torch.zeros(
            batch_size, self.text_maxlen, dtype=torch.int32, device=self.device
        )

    def optimize(self, onnx_graph):
        opt = Optimizer(onnx_graph, verbose=self.verbose)
        opt.info(self.name + ": original")
        opt.select_outputs([0])  # delete graph output#1
        opt.cleanup()
        opt.info(self.name + ": remove output[1]")
        opt.fold_constants()
        opt.info(self.name + ": fold constants")
        opt.infer_shapes()
        opt.info(self.name + ": shape inference")
        opt.select_outputs(
            [0], names=["text_embeddings"]
        )  # rename network output
        opt.info(self.name + ": remove output[0]")
        opt_onnx_graph = opt.cleanup(return_onnx=True)
        opt.info(self.name + ": finished")
        return opt_onnx_graph


def make_CLIP(
    version, hf_token, device, verbose, max_batch_size, inpaint=False
):
    return CLIP(
        hf_token=hf_token,
        device=device,
        verbose=verbose,
        path=get_path(version, inpaint=inpaint),
        max_batch_size=max_batch_size,
        embedding_dim=get_embedding_dim(version),
    )


class UNet(BaseModel):
    def __init__(
        self,
        hf_token,
        fp16=False,
        device="cuda",
        verbose=False,
        path="",
        max_batch_size=16,
        embedding_dim=768,
        text_maxlen=77,
        unet_dim=4,
    ):
        super(UNet, self).__init__(
            hf_token,
            fp16=fp16,
            device=device,
            verbose=verbose,
            path=path,
            max_batch_size=max_batch_size,
            embedding_dim=embedding_dim,
            text_maxlen=text_maxlen,
        )
        self.unet_dim = unet_dim
        self.name = "UNet"

    def get_model(self):
        model_opts = (
            {"revision": "fp16", "torch_dtype": torch.float16}
            if self.fp16
            else {}
        )
        return UNet2DConditionModel.from_pretrained(
            self.path,
            subfolder="unet",
            use_auth_token=self.hf_token,
            **model_opts,
        ).to(self.device)

    def get_input_names(self):
        return ["sample", "timestep", "encoder_hidden_states"]

    def get_output_names(self):
        return ["latent"]

    def get_dynamic_axes(self):
        return {
            "sample": {0: "2B", 2: "H", 3: "W"},
            "encoder_hidden_states": {0: "2B"},
            "latent": {0: "2B", 2: "H", 3: "W"},
        }

    def get_input_profile(
        self, batch_size, image_height, image_width, static_batch, static_shape
    ):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        (
            min_batch,
            max_batch,
            _,
            _,
            _,
            _,
            min_latent_height,
            max_latent_height,
            min_latent_width,
            max_latent_width,
        ) = self.get_minmax_dims(
            batch_size, image_height, image_width, static_batch, static_shape
        )
        return {
            "sample": [
                (
                    2 * min_batch,
                    self.unet_dim,
                    min_latent_height,
                    min_latent_width,
                ),
                (2 * batch_size, self.unet_dim, latent_height, latent_width),
                (
                    2 * max_batch,
                    self.unet_dim,
                    max_latent_height,
                    max_latent_width,
                ),
            ],
            "encoder_hidden_states": [
                (2 * min_batch, self.text_maxlen, self.embedding_dim),
                (2 * batch_size, self.text_maxlen, self.embedding_dim),
                (2 * max_batch, self.text_maxlen, self.embedding_dim),
            ],
        }

    def get_shape_dict(self, batch_size, image_height, image_width):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        return {
            "sample": (
                2 * batch_size,
                self.unet_dim,
                latent_height,
                latent_width,
            ),
            "encoder_hidden_states": (
                2 * batch_size,
                self.text_maxlen,
                self.embedding_dim,
            ),
            "latent": (2 * batch_size, 4, latent_height, latent_width),
        }

    def get_sample_input(self, batch_size, image_height, image_width):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        dtype = torch.float16 if self.fp16 else torch.float32
        return (
            torch.randn(
                2 * batch_size,
                self.unet_dim,
                latent_height,
                latent_width,
                dtype=torch.float32,
                device=self.device,
            ),
            torch.tensor([1.0], dtype=torch.float32, device=self.device),
            torch.randn(
                2 * batch_size,
                self.text_maxlen,
                self.embedding_dim,
                dtype=dtype,
                device=self.device,
            ),
        )


def make_UNet(
    version, hf_token, device, verbose, max_batch_size, inpaint=False
):
    return UNet(
        hf_token=hf_token,
        fp16=True,
        device=device,
        verbose=verbose,
        path=get_path(version, inpaint=inpaint),
        max_batch_size=max_batch_size,
        embedding_dim=get_embedding_dim(version),
        unet_dim=(9 if inpaint else 4),
    )


class VAE(BaseModel):
    def __init__(
        self, hf_token, device, verbose, path, max_batch_size, embedding_dim
    ):
        super(VAE, self).__init__(
            hf_token,
            device=device,
            verbose=verbose,
            path=path,
            max_batch_size=max_batch_size,
            embedding_dim=embedding_dim,
        )
        self.name = "VAE decoder"

    def get_model(self):
        vae = AutoencoderKL.from_pretrained(
            self.path, subfolder="vae", use_auth_token=self.hf_token
        ).to(self.device)
        vae.forward = vae.decode
        return vae

    def get_input_names(self):
        return ["latent"]

    def get_output_names(self):
        return ["images"]

    def get_dynamic_axes(self):
        return {
            "latent": {0: "B", 2: "H", 3: "W"},
            "images": {0: "B", 2: "8H", 3: "8W"},
        }

    def get_input_profile(
        self, batch_size, image_height, image_width, static_batch, static_shape
    ):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        (
            min_batch,
            max_batch,
            _,
            _,
            _,
            _,
            min_latent_height,
            max_latent_height,
            min_latent_width,
            max_latent_width,
        ) = self.get_minmax_dims(
            batch_size, image_height, image_width, static_batch, static_shape
        )
        return {
            "latent": [
                (min_batch, 4, min_latent_height, min_latent_width),
                (batch_size, 4, latent_height, latent_width),
                (max_batch, 4, max_latent_height, max_latent_width),
            ]
        }

    def get_shape_dict(self, batch_size, image_height, image_width):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        return {
            "latent": (batch_size, 4, latent_height, latent_width),
            "images": (batch_size, 3, image_height, image_width),
        }

    def get_sample_input(self, batch_size, image_height, image_width):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        return torch.randn(
            batch_size,
            4,
            latent_height,
            latent_width,
            dtype=torch.float32,
            device=self.device,
        )


def make_VAE(
    version, hf_token, device, verbose, max_batch_size, inpaint=False
):
    return VAE(
        hf_token=hf_token,
        device=device,
        verbose=verbose,
        path=get_path(version, inpaint=inpaint),
        max_batch_size=max_batch_size,
        embedding_dim=get_embedding_dim(version),
    )


class TorchVAEEncoder(torch.nn.Module):
    def __init__(self, token, device, path):
        super().__init__()
        self.path = path
        self.vae_encoder = AutoencoderKL.from_pretrained(
            self.path, subfolder="vae", use_auth_token=token
        ).to(device)

    def forward(self, x):
        return self.vae_encoder.encode(x).latent_dist.sample()


class VAEEncoder(BaseModel):
    def __init__(
        self, hf_token, device, verbose, path, max_batch_size, embedding_dim
    ):
        super(VAEEncoder, self).__init__(
            hf_token,
            device=device,
            verbose=verbose,
            path=path,
            max_batch_size=max_batch_size,
            embedding_dim=embedding_dim,
        )
        self.name = "VAE encoder"

    def get_model(self):
        vae_encoder = TorchVAEEncoder(self.hf_token, self.device, self.path)
        return vae_encoder

    def get_input_names(self):
        return ["images"]

    def get_output_names(self):
        return ["latent"]

    def get_dynamic_axes(self):
        return {
            "images": {0: "B", 2: "8H", 3: "8W"},
            "latent": {0: "B", 2: "H", 3: "W"},
        }

    def get_input_profile(
        self, batch_size, image_height, image_width, static_batch, static_shape
    ):
        assert batch_size >= self.min_batch and batch_size <= self.max_batch
        min_batch = batch_size if static_batch else self.min_batch
        max_batch = batch_size if static_batch else self.max_batch
        self.check_dims(batch_size, image_height, image_width)
        (
            min_batch,
            max_batch,
            min_image_height,
            max_image_height,
            min_image_width,
            max_image_width,
            _,
            _,
            _,
            _,
        ) = self.get_minmax_dims(
            batch_size, image_height, image_width, static_batch, static_shape
        )

        return {
            "images": [
                (min_batch, 3, min_image_height, min_image_width),
                (batch_size, 3, image_height, image_width),
                (max_batch, 3, max_image_height, max_image_width),
            ],
        }

    def get_shape_dict(self, batch_size, image_height, image_width):
        latent_height, latent_width = self.check_dims(
            batch_size, image_height, image_width
        )
        return {
            "images": (batch_size, 3, image_height, image_width),
            "latent": (batch_size, 4, latent_height, latent_width),
        }

    def get_sample_input(self, batch_size, image_height, image_width):
        self.check_dims(batch_size, image_height, image_width)
        return torch.randn(
            batch_size,
            3,
            image_height,
            image_width,
            dtype=torch.float32,
            device=self.device,
        )


def make_VAEEncoder(
    version, hf_token, device, verbose, max_batch_size, inpaint=False
):
    return VAEEncoder(
        hf_token=hf_token,
        device=device,
        verbose=verbose,
        path=get_path(version, inpaint=inpaint),
        max_batch_size=max_batch_size,
        embedding_dim=get_embedding_dim(version),
    )


def make_tokenizer(version, hf_token):
    return CLIPTokenizer.from_pretrained(
        get_path(version), subfolder="tokenizer", use_auth_token=hf_token
    )
