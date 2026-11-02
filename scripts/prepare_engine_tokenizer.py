#!/usr/bin/env python3
"""Pin tokenizer assets to the downloaded model; no inference or model changes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from coder_engine_tokens import TOKENIZER_REPOSITORY, TOKENIZER_REVISION, MODEL_DIGEST, ASSET_HASHES


def request(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response: return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--ollama-url', required=True)
    parser.add_argument('--model', default='qwen3.6-35b-a3b-gguf:latest')
    args = parser.parse_args()
    if args.destination.exists(): parser.error('Destination exists; assets are immutable')
    tags = json.loads(request(args.ollama_url + '/api/tags'))['models']
    entry = next(m for m in tags if m['name'] == args.model)
    if entry['digest'] != MODEL_DIGEST: raise ValueError('Downloaded model digest changed')
    show = json.loads(request(args.ollama_url + '/api/show', {'model': args.model}))
    if '{%' not in show['template']: raise ValueError('This qualified counter requires the installed Jinja template')
    data = {'tokenizer.json': request(f'https://huggingface.co/{TOKENIZER_REPOSITORY}/resolve/{TOKENIZER_REVISION}/tokenizer.json'),
            'chat_template.jinja': show['template'].encode()}
    if {n: hashlib.sha256(v).hexdigest() for n,v in data.items()} != ASSET_HASHES:
        raise ValueError('Downloaded assets do not match their pinned checksums')
    args.destination.mkdir(parents=True)
    for name, content in data.items(): (args.destination / name).write_bytes(content)
    manifest = {'repository': TOKENIZER_REPOSITORY, 'revision': TOKENIZER_REVISION, 'model_digest': entry['digest'],
                'model': args.model, 'files': {n: hashlib.sha256(v).hexdigest() for n,v in data.items()}}
    (args.destination / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest))


if __name__ == '__main__': main()
