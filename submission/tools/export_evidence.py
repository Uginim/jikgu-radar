"""Prepare public JSONL from the integration owner's already-redacted chronological export.

Do not accept unredacted transport logs. Keep each source event in order. Preserve text
bodies, scores, errors and provenance; publish a fingerprint rather than republishing a
retailer's images. Neither this tool nor the resulting index sets an application verdict.
"""
import argparse
import base64
import hashlib
import io
import gzip
import json
from pathlib import Path
from datetime import datetime
from typing import Any
from replay_images import variants

from PIL import Image


def digest(data):
    return hashlib.sha256(data).hexdigest()


def public_value(value: Any, key='') -> Any:
    if isinstance(value, dict):
        return {k: public_value(v, k) for k, v in value.items() if k != 'body_base64'}
    if isinstance(value, list):
        return [public_value(v, key) for v in value]
    if not isinstance(value, str):
        return value
    if value.startswith('data:image/') or (key == 'image' and len(value) > 100 and not value.startswith(('{', '['))):
        data = base64.b64decode(value.split(',', 1)[-1], validate=True)
        with Image.open(io.BytesIO(data)) as image:
            return {'image_sha256': digest(data), 'bytes': len(data), 'format': image.format,
                    'width': image.width, 'height': image.height}
    if key in ('body', 'body_utf8') and value.lstrip().startswith(('{', '[')):
        raw = json.loads(value)
        converted = public_value(raw)
        if raw != converted:
            return json.dumps(converted, ensure_ascii=False, separators=(',', ':'))
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='sanitized-… directory')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=True, parents=True)
    index = {'schema': 'judge-records/v1', 'scenes': []}
    merged = []
    for number in (1, 2, 3):
        name = f'scenario-{number:02d}.jsonl'
        data = (args.source / name).read_bytes()
        rows = [json.loads(line) for line in data.splitlines()]
        head = rows[0]
        if head.get('schema') != 'chronological-original-events/v1' or head.get('gaps'):
            raise ValueError(f'Incomplete or unexpected integration export: {name}')
        public = [public_value(row) for row in rows]
        public[0]['public_copy'] = {'integration_export_sha256': digest(data),
                                    'images': 'SHA-256, format, byte count and dimensions instead of third-party image bytes',
                                    'body_base64': 'Redundant base64 copy omitted. Use body_utf8; original byte hashes retained.',
                                    'assets': 'Filenames in original inventory are references, not redistributed files.'}
        operations = {}
        for asset in (args.source / name.replace('.jsonl', '-assets')).glob('input-*'):
            raw = asset.read_bytes()
            for operation, variant in variants(raw):
                operations[digest(variant)] = [digest(raw), operation]
        required = {im['image_sha256'] for row in public[1:] if row['record'].get('event') == 'upstream_request'
                    for im in [json.loads(row['record']['body_utf8']).get('image')] if isinstance(im, dict)}
        public[0]['public_copy']['image_operations'] = {h: operations[h] for h in sorted(required)}
        out = ''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n' for row in public).encode()
        (args.output / name).write_bytes(out)
        index['scenes'].append({'file': name, 'source_id': head['source_id'], 'case_id': head['case_id'],
                                'lines': len(public), 'bytes': len(out), 'sha256': digest(out),
                                'integration_export_sha256': digest(data)})
        for line, row in enumerate(public[1:], 2):
            merged.append((row['observed_at'], name, line, row))
    merged.sort(key=lambda row: datetime.fromisoformat(row[0]).timestamp())
    index['timeline'] = [{'time': t, 'file': f, 'line': n} for t, f, n, _ in merged]
    combined = b''.join((json.dumps({'scene_file': f, 'scene_line': n, 'event': r}, ensure_ascii=False, separators=(',', ':')) + '\n').encode() for _, f, n, r in merged)
    combined_name = 'all-events.jsonl.gz'
    (args.output / combined_name).write_bytes(gzip.compress(combined, mtime=0))
    index['combined'] = {'file': combined_name, 'events': len(merged), 'uncompressed_sha256': digest(combined)}
    (args.output / 'run-index.json').write_text(json.dumps(index, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
