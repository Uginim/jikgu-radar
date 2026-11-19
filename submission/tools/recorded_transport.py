"""Local transport serving only model/OCR responses whose request matches a filmed request."""
import base64
import json
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast
from replay_images import data_url, reference, sha, variants


class Scene:
    def __init__(self, path: Path):
        self.name = path.stem
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.actions = []
        self.images = {}
        self.aliases = {}
        self.responses = {}
        self.expected = Counter()
        self.observed = Counter()
        self.last_verdict = None
        self.added = None
        wire = {}
        ops = records[0]['public_copy']['image_operations']
        for row in records[1:]:
            r = row['record']; event = r['event']
            if event == 'browser_http_request':
                body = json.loads(r['body']) if r.get('body') else {}
                self.actions.append((r['url'].split('/cases', 1)[1].split('/')[-1], body))
                for info in [*body.get('images', []), body.get('image')]:
                    if isinstance(info, dict):
                        source = info['image_sha256']
                        raw = reference(info)
                        self.images[source] = data_url(raw, info['format'])
                        lookup = {tuple(v): k for k, v in ops.items()}
                        for transform, raster in variants(raw):
                            if (source, transform) in lookup:
                                self.aliases[sha(raster)] = lookup[source, transform]
            elif event == 'upstream_request':
                body = json.loads(r['body_utf8'])
                if 'image' in body:
                    body['image'] = body['image']['image_sha256']
                key = self.key(r['path'], body)
                self.expected[key] += 1
                wire[r['call']] = key
            elif event == 'upstream_response':
                key = wire[r['call']]
                answer = (r['upstream_status'], json.loads(r['body_utf8']))
                if key in self.responses and self.responses[key] != answer:
                    raise ValueError(f'{path.name}: conflicting responses for the same recorded request')
                self.responses[key] = answer
            elif event == 'final':
                self.last_verdict = r['verdict']
            elif event == 'followup_material':
                self.added = r.get('added')
        if not self.last_verdict or self.responses.keys() != self.expected.keys():
            raise ValueError(f'{path.name}: incomplete evidence')

    @staticmethod
    def key(path, body):
        return path, json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class TapeServer(ThreadingHTTPServer):
    def __init__(self, scenes):
        super().__init__(('127.0.0.1', 0), TapeHandler)
        self.scenes = scenes
        self.misses = []
        self.scene = None
        self.thread = Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown(); self.server_close(); self.thread.join()


class TapeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        server = cast(TapeServer, self.server)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        raster = body.pop('image', None)
        image_hash = sha(base64.b64decode(raster, validate=True)) if raster is not None else None
        scenes = [server.scene] if server.scene is not None else server.scenes
        for scene in scenes:
            if image_hash is not None:
                body['image'] = scene.aliases.get(image_hash, image_hash)
            key = scene.key(self.path, body)
            if key in scene.responses:
                status, result = scene.responses[key]
                scene.observed[key] += 1
                break
        else:
            status, result = 422, {'error': 'No recorded response matches this request', 'path': self.path}
            server.misses.append((self.path, sha(json.dumps(body, sort_keys=True).encode())[:12]))
        data = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
