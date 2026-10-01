#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Single-model MLX embedding engine; routing and provider translation stay in SR."""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hmac
import io
import json
import math
from pathlib import Path
import socket
import ipaddress
import struct
import sys
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image
import uvicorn

MAX_BODY = 16 * 1024 * 1024
MAX_IMAGE = 10 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000


class InputError(ValueError):
    pass


def check_image_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise InputError('images require an HTTPS URL or inline data')
    for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(entry[4][0]).is_global:
            raise InputError('image URLs must resolve to public addresses')


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_image(url, max_pixels):
    if not isinstance(url, str):
        raise InputError('image URL must be a string')
    if url.startswith('data:image/'):
        prefix, separator, encoded = url.partition(',')
        if not separator or not prefix.endswith(';base64'):
            raise InputError('inline images require base64 encoding')
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise InputError('invalid image base64') from None
    else:
        check_image_url(url)
        opener = urllib.request.build_opener(CheckedRedirect())
        with opener.open(url, timeout=20) as response:
            data = response.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise InputError('image exceeds 10 MiB')
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > Image.MAX_IMAGE_PIXELS:
            raise InputError('image exceeds pixel limit')
        image = original.convert('RGB')
    factor = min(1.0, math.sqrt(max_pixels / (image.width * image.height)))
    if factor < 1:
        image = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))))
    return image


def parse_input(body, settings):
    if not isinstance(body, dict) or set(body) - {'model', 'input', 'messages', 'dimensions', 'encoding_format', 'user'}:
        raise InputError('unsupported embedding fields')
    if body.get('model') != settings['model_id']:
        raise InputError('unknown model')
    if ('input' in body) == ('messages' in body):
        raise InputError('provide exactly one of input or messages')
    dimensions = body.get('dimensions', settings['dimensions'])
    if type(dimensions) is not int or not settings['min_dimensions'] <= dimensions <= settings['dimensions']:
        raise InputError('unsupported embedding dimensions')
    encoding = body.get('encoding_format', 'float')
    if encoding not in {'float', 'base64'}:
        raise InputError('encoding_format must be float or base64')
    if 'user' in body and (not isinstance(body['user'], str) or len(body['user']) > 1024):
        raise InputError('invalid user')
    if 'input' in body:
        inputs = [body['input']] if isinstance(body['input'], str) else body['input']
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= settings['max_batch_size']:
            raise InputError('embedding batch exceeds the configured limit')
        if any(not isinstance(text, str) or not text.strip() for text in inputs):
            raise InputError('this engine accepts text, not token IDs')
        items = [{'text': text} for text in inputs]
    else:
        messages = body['messages']
        if not isinstance(messages, list) or not 1 <= len(messages) <= 2:
            raise InputError('messages require one user input and optional system instruction')
        instruction = None
        if len(messages) == 2:
            system = messages[0]
            if not isinstance(system, dict) or set(system) != {'role', 'content'} or system['role'] != 'system' or not isinstance(system['content'], str):
                raise InputError('invalid embedding instruction')
            instruction = system['content']
        message = messages[-1]
        if not isinstance(message, dict) or set(message) != {'role', 'content'} or message['role'] != 'user':
            raise InputError('embedding requires a user input')
        content = message['content']
        if isinstance(content, str): content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list) or not content:
            raise InputError('empty multimodal input')
        texts, images = [], []
        for part in content:
            if not isinstance(part, dict): raise InputError('invalid content block')
            if part.get('type') == 'text' and set(part) == {'type', 'text'} and isinstance(part['text'], str):
                texts.append(part['text'])
            elif part.get('type') == 'image_url' and set(part) == {'type', 'image_url'}:
                image = part['image_url']
                if not isinstance(image, dict) or set(image) - {'url', 'detail'} or image.get('detail', 'auto') != 'auto':
                    raise InputError('image detail must be auto')
                if images: raise InputError('one image per sample is supported')
                images.append(load_image(image.get('url'), settings['max_pixels']))
            else:
                raise InputError('supported content types are text and image_url')
        if not images and not any(text.strip() for text in texts): raise InputError('empty input')
        items = [{'text': texts, 'image': images}]
        if instruction is not None: items[0]['instruction'] = instruction
    return items, dimensions, encoding


def create_app(settings):
    key = Path(settings['secret_file']).read_text().strip()
    if not key: raise ValueError('empty embedding credential')
    model_path = Path(settings['model'])
    verification = json.loads((model_path / 'verification.json').read_text())
    if verification['revision'] != settings['revision']: raise ValueError('model revision mismatch')
    for item in verification['files']:
        target = (model_path / item['file']).resolve()
        if not target.is_relative_to(model_path.resolve()) or target.stat().st_size != item['bytes']:
            raise ValueError('verified model files changed')
    executor = ThreadPoolExecutor(max_workers=1)
    engine = {}
    busy = False

    def load():
        import mlx.core as mx
        from mlx_embeddings import load as load_model
        mx.set_memory_limit(settings['memory_limit_gib'] * 2**30)
        mx.set_cache_limit(512 * 2**20)
        model, processor = load_model(str(model_path), tokenizer_config={'trust_remote_code': False, 'embedding_max_length': settings['max_input_tokens'] + 1, 'max_pixels': settings['max_pixels']})
        engine.update(mx=mx, model=model, processor=processor)

    def embed(body):
        mx = engine['mx']
        items, dimensions, encoding = parse_input(body, settings)
        inputs = engine['processor'].prepare_embedding_inputs(items)
        if inputs['input_ids'].shape[-1] > settings['max_input_tokens']:
            raise InputError('input exceeds the configured token limit; no truncation is served')
        # Independent embedding samples must not reuse Qwen generation position
        # state. The pinned runtime otherwise fails when a shorter text follows
        # a longer one, and can carry image positions into a later text request.
        engine['model'].language_model._position_ids = None
        engine['model'].language_model._rope_deltas = None
        vectors = engine['model'](**inputs).text_embeds.astype(mx.float32)[:, :dimensions]
        vectors = vectors / mx.sqrt(mx.sum(vectors * vectors, axis=-1, keepdims=True))
        mx.eval(vectors)
        data = []
        for index, vector in enumerate(vectors.tolist()):
            if not all(math.isfinite(v) for v in vector): raise RuntimeError('non-finite embedding')
            value = base64.b64encode(struct.pack('<' + 'f' * dimensions, *vector)).decode() if encoding == 'base64' else vector
            data.append({'object': 'embedding', 'index': index, 'embedding': value})
        tokens = int(inputs['attention_mask'].sum().item())
        return {'object': 'list', 'model': settings['model_id'], 'data': data, 'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.get_running_loop().run_in_executor(executor, load)
        yield
        executor.shutdown(wait=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + key):
            return JSONResponse({'error': {'message': 'unauthorized'}}, status_code=401)
        return await call_next(request)

    @app.get('/health')
    async def health():
        return {'status': 'ready', 'model': settings['model_id'], 'revision': settings['revision'], 'dimensions': settings['dimensions'], 'busy': busy}

    @app.post('/v1/embeddings')
    async def embeddings(request: Request):
        nonlocal busy
        if busy: return JSONResponse({'error': {'message': 'embedding engine busy'}}, status_code=429)
        busy = True
        try:
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY: return JSONResponse({'error': {'message': 'request too large'}}, status_code=413)
                chunks.append(chunk)
            body = json.loads(b''.join(chunks))
            result = await asyncio.get_running_loop().run_in_executor(executor, embed, body)
            return result
        except (InputError, json.JSONDecodeError) as error:
            return JSONResponse({'error': {'message': str(error), 'type': 'invalid_request_error'}}, status_code=400)
        except Exception as error:
            # Do not expose request content, image URLs or credentials in logs.
            print('embedding failure:', type(error).__name__, flush=True)
            return JSONResponse({'error': {'message': 'embedding inference failed'}}, status_code=500)
        finally:
            busy = False

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--settings', type=Path)
    source.add_argument('--config', type=Path)
    parser.add_argument('--backend')
    args = parser.parse_args()
    if args.config:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        from stack_config import StackConfig
        settings = StackConfig(args.config).mlx_settings(args.backend)
    else:
        settings = json.loads(args.settings.read_text())
    uvicorn.run(create_app(settings), host=settings['listen'], port=settings['port'], workers=1, access_log=False, timeout_keep_alive=5)


if __name__ == '__main__':
    main()
