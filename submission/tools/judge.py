"""Run the frozen application; replay only upstream inference, never its final verdicts."""
import argparse
import html
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'submission/runtime'
http = build_opener(ProxyHandler({}))
NAMES = ('Nitrotest', 'Stimerex Hardcore', 'Blackmores Iron Rapi-Melt')


def api(base, path, body=None):
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
    with http.open(req, timeout=30) as response:
        return json.load(response)


def await_state(base, route, predicate):
    end = time.monotonic() + 60
    while time.monotonic() < end:
        snap = api(base, route)
        if predicate(snap):
            return snap
        time.sleep(.08)
    raise RuntimeError(f'{route}: expected case state not reached')


def rerun(scene, base):
    before = {}
    route = None
    reviewed = lambda s: s.get('status') == 'done'
    for action, original in scene.actions:
        body = dict(original)
        if not route:
            body['url'] = body['url'].replace('[가림]', base)
            body['images'] = [scene.images[i['image_sha256']] for i in body.get('images', [])]
            case_id = api(base, '/cases', body)['case_id']
            route = '/cases/' + case_id
        elif action == 'answer':
            s = await_state(base, route, lambda s: s.get('question'))
            if body['question_id'] != s['question']['id']:
                raise RuntimeError('Recorded question differs from the current question')
            api(base, route + '/answer', body)
        elif action == 'inquiry-draft':
            await_state(base, route, reviewed)
            draft = api(base, route + '/inquiry-draft', {})
            if draft.get('source') != 'zoro':
                raise RuntimeError('Recorded inquiry was not produced by the matching generation response')
        elif action == 'follow-up':
            snap = await_state(base, route, reviewed)
            before = snap['extras']['label_evidence']['review_requirements']
            body['evidence_id'] = snap['extras']['label_evidence']['review_requirements']['evidence_id']
            body['image'] = scene.images[body['image']['image_sha256']]
            api(base, route + '/follow-up', body)
            reviewed = lambda s: s.get('status') == 'done' and s.get('follow_up', {}).get('state') == 'reviewed'
        else:
            raise ValueError(f'Unknown recorded browser action: {action}')
    if route is None:
        raise ValueError('Missing original case input')
    snap = await_state(base, route, reviewed)
    after = snap['extras']['label_evidence']['review_requirements']
    resolved = {item['check'] for item in after.get('resolved_by_followup', [])}
    if resolved:
        old = set(before['missing']); new = set(after['missing'])
        if old - new != resolved or new - old or after['complete'] or snap['verdict']['key'] != 'review':
            raise RuntimeError('Follow-up must explain precisely which old checks no longer remain, without approving the case')
    observed = snap['verdict']
    if observed != scene.last_verdict and not (resolved and observed['key'] == scene.last_verdict['key'] and observed['label'] == scene.last_verdict['label']):
        raise RuntimeError(f'{scene.name}: verdict differs from the recorded film: {observed}')
    if scene.added is not None and (snap.get('follow_up', {}).get('added') != scene.added or
                                    snap['follow_up']['state'] != 'reviewed'):
        raise RuntimeError('The new evidence was not evaluated on the same case')
    if scene.expected != scene.observed:
        missing = [(k[0], n) for k, n in (scene.expected - scene.observed).items()]
        raise RuntimeError(f'{scene.name}: inference call count differs: missing {missing}')
    return {'scene': scene.name, 'case_id': snap['case_id'], 'verdict': snap['verdict']['key'],
            'label': observed['label'], 'why': observed['why'], 'calls': sum(scene.observed.values()),
            'added': scene.added or [], 'followup_checks_resolved': sorted(resolved), 'remaining': after['missing']}


def run(mode, port):
    from recorded_transport import Scene, TapeServer
    from replay_images import sha
    from fastapi.responses import HTMLResponse
    import uvicorn

    # Bind before launching the server: never use a pre-existing local service by mistake.
    with socket.socket() as listener, tempfile.TemporaryDirectory(prefix='jikgu-judge-') as scratch:
        listener.bind(('127.0.0.1', port)); listener.listen(128)
        base = 'http://127.0.0.1:' + str(listener.getsockname()[1])
        live = mode == 'live'
        endpoint = os.environ.get('ZORO_URL') if live else None
        key = os.environ.get('ZORO_API_KEY', '') if live else ''
        if live and not endpoint:
            raise RuntimeError('Live requires ZORO_URL; no GPU or recording fallback was used')
        for k in tuple(os.environ):
            if k.startswith(('NEXTASK_', 'ZORO_', 'OCR_', 'MFDS_', 'PUBCHEM_', 'DSLD_')) or k in ('NEXT_POLICY', 'FIXED_PIPELINE', 'DATA_GO_KR_KEY'):
                del os.environ[k]
        logdir = ROOT / 'submission/logs'
        release = json.loads((ROOT / 'submission/release.json').read_text(encoding='utf-8'))
        for name, digest in release['runtime_files'].items():
            if sha((RUNTIME / name).read_bytes()) != digest:
                raise ValueError(f'File does not match the selected submission release: {name}')
        version = release['submission_release']
        film = release['filmed_release']
        scenes = []
        if not live:
            for row in json.loads((logdir / 'run-index.json').read_text())['scenes']:
                path = logdir / row['file']
                if sha(path.read_bytes()) != row['sha256']:
                    raise ValueError(f'Changed evidence: {row["file"]}')
                scenes.append(Scene(path))
        tape = None if live else TapeServer(scenes)
        if tape is not None:
            endpoint = f'http://127.0.0.1:{tape.server_port}'
            # Reject external DNS and sockets even if a future lookup ignores its offline flag.
            def loopback_only(event, args):
                if event == 'socket.getaddrinfo' and args[0] not in ('127.0.0.1', 'localhost', '::1'):
                    raise PermissionError('Recorded replay permits only loopback connections')
                if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('127.0.0.1', '::1'):
                    raise PermissionError('Recorded replay permits only loopback connections')
            sys.addaudithook(loopback_only)
        os.environ.update(ZORO_MODE='remote', ZORO_URL=str(endpoint),
                          ZORO_API_KEY=key, ZORO_RETRIES='1', OCR_ENGINE='remote', MFDS_LIVE='0',
                          DSLD_LIVE='0', PUBCHEM_LIVE='0', NEXTASK_SYSTEM2='0', NEXTASK_AGENT_RECOVERY='1',
                          NEXTASK_INGREDIENT_KB='1', NEXTASK_SITE_GENERIC='1', NEXTASK_SEARCH_FIRST='1',
                          NEXTASK_LOG_DIR=scratch, NEXTASK_STORE=str(Path(scratch) / 'store.json'))
        sys.path.insert(0, str(RUNTIME))
        from nextask.core.web.app import app
        results = []

        @app.get('/judge/results')
        def results_json():
            return {'mode': 'live' if live else 'recorded-upstream', 'code': version, 'source_id': release['submission_source_id'],
                    'recorded_inputs': None if live else film, 'results': results}

        @app.get('/', response_class=HTMLResponse)
        def guide():
            title = '실제 GPU 연결 설정' if live else 'GPU 없이 촬영 응답 재생'
            rows = ''.join(f'<li><strong>{html.escape(n)}</strong>: {html.escape(r["label"])} · <a href="/cases/{r["case_id"]}/report">새 사건 로그</a></li>' for n, r in zip(NAMES, results))
            note = ('상품 탭에서 확장을 열어 검사하세요. 첫 추론이 성공하기 전에는 GPU 연결을 검증한 것이 아닙니다.' if live
                    else '촬영된 입력만 지원합니다. 사진 픽셀 대신 식별용 그림을 쓰고 원래의 OCR·판단 응답을 돌려줍니다.')
            return f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>직구레이더 · {title}</title><main><h1>직구레이더 | {title}</h1><p>제출 코드: {html.escape(version)} · 영상·로그: {html.escape(film)}</p><p>Chrome 확장의 서버 주소: <code>{base}</code></p><ol>{rows}</ol><p>{note} 마켓에 등록·발송하지 않습니다.</p></main></html>'

        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        browser = None
        try:
            end = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < end:
                time.sleep(.05)
            if not server.started:
                raise RuntimeError('Local API did not start')
            print(f'제출 코드 {version} · 영상·로그 {film} | {"실제 GPU 연결 설정" if live else "촬영 응답 전용 · GPU 호출 없음"} | {base}', flush=True)
            if tape is not None:
                for name, scene in zip(NAMES, scenes, strict=True):
                    tape.scene = scene
                    result = rerun(scene, base)
                    results.append(result)
                    print(f'{name}: {result["verdict"]} | {result["calls"]}개 기록 응답 | {base}/cases/{result["case_id"]}/report', flush=True)
                tape.scene = None
                if tape.misses:
                    raise RuntimeError(f'Unmatched inference requests: {tape.misses}')
                print('세 상품 판정·호출 수 일치. Rapi-Melt 추가 성분도 일치.', flush=True)
            if mode != 'check':
                executable = os.environ.get('JUDGE_CHROME_BIN') or shutil.which('chromium') or shutil.which('chromium-browser')
                if executable:
                    ext = RUNTIME / 'extension'
                    browser = subprocess.Popen([executable, '--no-first-run', '--disable-sync', '--disable-background-networking',
                                                f'--user-data-dir={Path(scratch) / "chrome"}', f'--disable-extensions-except={ext}',
                                                f'--load-extension={ext}', base], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print('전용 브라우저에 확장과 결과 열기를 요청했습니다. 종료: Ctrl+C', flush=True)
                else:
                    print('Chrome에서 위 주소를 여세요. 확장 설치: START_HERE_KO.md 3번. 종료: Ctrl+C', flush=True)
                try:
                    thread.join()
                except KeyboardInterrupt:
                    pass
        finally:
            if browser and browser.poll() is None:
                browser.terminate()
                try:
                    browser.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print('전용 Chromium 창을 닫아 주세요.', file=sys.stderr)
            server.should_exit = True
            thread.join(5)
            if tape:
                if tape.misses:
                    print('매칭 실패(요청 종류·지문):', tape.misses, file=sys.stderr)
                tape.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('replay', 'check', 'live'), default='replay', nargs='?')
    p.add_argument('--port', type=int)
    args = p.parse_args()
    run(args.mode, args.port if args.port is not None else 0 if args.mode == 'check' else 8000)
