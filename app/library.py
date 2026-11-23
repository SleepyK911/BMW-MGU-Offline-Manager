"""Source configuration, version snapshots, and deployable ZIPs."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import zipfile
from argparse import Namespace
from pathlib import Path

import build_mgu_usb as builder

CHANNELS = {'catalog': '发布目录', 'branch': '分支', 'tags': '稳定标签'}
REPO_PATTERN = r'[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9_.-]{1,100}'
REPORT = 'MGU-LIBRARY-STATUS.json'
REPORT_FILES = {REPORT, 'MGU-LIBRARY-STATUS.txt', 'MGU-LIBRARY-SHA256.txt'}
ROOT = Path(__file__).resolve().parent


def normalize_repo(value):
    value = value.strip()
    if value.lower().startswith('github:'):
        value = value[7:]
    elif '://' in value:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ('https', 'http') or parsed.netloc.lower() != 'github.com':
            raise builder.BuildFailure('请输入 GitHub 仓库链接或 owner/repo。')
        value = parsed.path.lstrip('/')
    value = value.rstrip('/')
    if value.endswith('.git'):
        value = value[:-4]
    if not re.fullmatch(REPO_PATTERN, value) or value.split('/')[1] in ('.', '..'):
        raise builder.BuildFailure('请输入 GitHub 仓库主页链接或 owner/repo。')
    return value


def make_source(value, channel, branch=''):
    repo = normalize_repo(value)
    if channel not in CHANNELS:
        raise builder.BuildFailure('请选择来源通道。')
    branch = (branch.strip() or 'HEAD') if channel == 'branch' else ''
    if branch and (len(branch) > 200 or branch.startswith('-') or '..' in branch or '@{' in branch or
                   any(not p or p.startswith('.') or p.endswith(('.', '.lock')) for p in branch.split('/')) or
                   re.search(r'[\s~^:?*\[\\\x00-\x1f\x7f]', branch)):
        raise builder.BuildFailure('分支名称无效。留空会使用仓库默认分支。')
    for default in builder.SOURCES:
        if repo.casefold() == default['repo'].casefold() and channel == default['channel'] and branch in ('', 'main'):
            return dict(default)
    key = f'{repo.casefold()}\n{channel}\n{branch}'
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    stem = re.sub(r'[^a-z0-9_-]+', '-', repo.lower().split('/')[1]).strip('-_')[:30]
    source = {'repo': repo, 'channel': channel, 'folder': f'custom-{stem or "repo"}-{digest}'}
    if branch:
        source['branch'] = branch
    return source


def detect_source(repo, proxy):
    known = next((s for s in builder.SOURCES if s['repo'].casefold() == repo.casefold()), None)
    if known:
        return dict(known)
    opener = builder.make_opener(proxy)
    source = make_source(repo, 'catalog')
    with tempfile.TemporaryDirectory(prefix='mgu-source-') as temp:
        try:
            builder.fetch_catalog(source, opener, Path(temp)/'catalog.json', attempts=1)
            return source
        except builder.BuildFailure as ex:
            if not isinstance(ex.__cause__, urllib.error.HTTPError) or ex.__cause__.code != 404:
                raise
        manifest = Path(temp)/'mgu-repo.json'
        try:
            url = f'https://raw.githubusercontent.com/{repo}/HEAD/mgu-repo.json'
            builder.download_file(url, manifest, opener, attempts=1, max_bytes=1_000_000)
        except builder.BuildFailure as ex:
            if isinstance(ex.__cause__, urllib.error.HTTPError) and ex.__cause__.code == 404:
                raise builder.BuildFailure('未找到发布目录或默认分支的 mgu-repo.json。若项目使用稳定标签或其他分支，请手动选通道。') from ex
            raise
        if builder.read_json(manifest).get('schema') != 1:
            raise builder.BuildFailure('该仓库的 mgu-repo.json 格式不受支持。')
    return make_source(repo, 'branch')


def complete_report(report, sources):
    rows = report.get('sources', [])
    return (bool(sources) and len(rows) == len(sources) and
            {r['folder'] for r in rows if r.get('status') == 'OK'} == {s['folder'] for s in sources})


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def by_folder(snapshot):
    return {item['folder']: item for item in (snapshot or {}).get('sources', [])}


def revision(row):
    if not row:
        return ''
    if row.get('channel') == 'catalog':
        return row.get('catalog_sha256')
    commit = row.get('commit')
    if row.get('channel') == 'tags' and commit:
        return (row.get('tag'), commit)
    return commit


def repo_version(row):
    if not row:
        return '—'
    commit = (row.get('commit') or '')[:9]
    if row['channel'] == 'catalog':
        preferred = {'00-mgu-store': 'robertn_store', '01-idrive-apps': 'waze-on-cluster', '06-jap': 'jap_sysmon'}
        version = dict(row.get('items', [])).get(preferred.get(row['folder']))
        return f'{version or "目录"} · {(row.get("catalog_sha256") or commit or "?")[:8]}'
    if row['channel'] == 'tags':
        return row.get('tag') or commit or '—'
    return commit or '—'


def archive_inventory(archive, fingerprints=False):
    with zipfile.ZipFile(archive) as z:
        roots = {name.split('/')[0] for name in z.namelist() if name and not name.startswith('__MACOSX/')}
        if len(roots) != 1:
            raise builder.BuildFailure('GitHub ZIP root is invalid')
        prefix = roots.pop() + '/'
        names = {i.filename[len(prefix):] for i in z.infolist() if i.filename.startswith(prefix) and not i.is_dir()
                 and not stat.S_ISLNK(i.external_attr >> 16) and included_tree_file(i.filename[len(prefix):])}

        def read(name):
            member = z.getinfo(prefix + name)
            if member.file_size > 1_000_000:
                raise builder.BuildFailure('Source manifest exceeds 1 MB')
            return json.loads(z.read(member))

        items = builder.tree_inventory(read('mgu-repo.json'), names, read)
        if fingerprints:
            fingerprint_tree(items, names, lambda name: z.open(prefix + name))
        return items


def included_tree_file(name):
    return not any(part in ('.git', 'node_modules', '.DS_Store') or part.startswith('._') for part in name.split('/'))


def fingerprint_tree(items, names, open_file):
    names = sorted(set(names) - {'mgu-repo.json', 'mgu-copy.json'})
    file_hashes = {}
    for component in items.values():
        entry = json.dumps(component['entry'], sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(entry)
        paths = component['paths']
        for name in names:
            if not any(not path or name == path or name.startswith(path + '/') for path in paths):
                continue
            if name not in file_hashes:
                with open_file(name) as stream:
                    file_hashes[name] = hashlib.file_digest(stream, 'sha256').digest()
            digest.update(name.encode('utf-8') + b'\0' + file_hashes[name])
        component['hash'] = digest.hexdigest()


def catalog_inventory(catalog):
    return {entry['id']: {'version': entry['version'], 'hash': entry['artifact']['sha256'],
                         'requires': entry.get('requires') or {}, 'provides': entry.get('mguProvides'),
                         'needs': entry.get('mguNeeds') or []} for entry in catalog['components']}


def check_online(cache, proxy, previous, snapshots, progress, sources):
    old = by_folder(previous)

    def inspect(source):
        opener = builder.make_opener(proxy)
        label = source['folder']
        try:
            row = dict(source, checked_utc=now(), state='checked')
            if source['channel'] == 'catalog':
                with tempfile.TemporaryDirectory(prefix='mgu-check-') as tmp:
                    path = Path(tmp)/'catalog.json'
                    catalog = builder.fetch_catalog(source, opener, path)
                    row.update(commit=catalog.get('commit'), catalog_sha256=builder.sha256(path),
                               items=[(c['id'], c['version']) for c in catalog['components']])
            else:
                ref = builder.resolve_ref(source, opener)
                row.update(commit=ref['sha'], tag=ref['tag'])
                known = next((r for r in snapshots.get(label, []) if r.get('commit') == ref['sha'] and r.get('items')), None)
                if known:
                    row['items'] = known['items']
                else:
                    try:
                        archive = builder.source_archive(source, ref, opener, cache, False, lambda _: None)
                        row['items'] = [(ident, item['version']) for ident, item in archive_inventory(archive).items()]
                    except Exception as ex:
                        if isinstance(ex, (zipfile.BadZipFile, EOFError)):
                            archive.unlink(missing_ok=True)
                        row['items_error'] = str(ex)
            return row
        except Exception as ex:
            row = dict(old.get(label, source))
            row.update(state='stale', error=str(ex))
            return row

    rows = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(inspect, source): source['folder'] for source in sources}
        for task in concurrent.futures.as_completed(tasks):
            label = tasks[task]
            rows[label] = task.result()
            progress(len(rows), len(sources), rows[label]['repo'])
    return {'generated_utc': now(), 'sources': [rows[source['folder']] for source in sources]}


def valid_report(root, verify_hash_list=False, expected=None):
    report = builder.read_json(root/REPORT)
    if not isinstance(report, dict) or not isinstance(report.get('sources'), list):
        raise builder.BuildFailure(f'缺少有效的 {REPORT}')
    configured = report.get('configured_sources', builder.SOURCES)
    if not isinstance(configured, list) or not 1 <= len(configured) <= 64:
        raise builder.BuildFailure('部署包的来源清单无效')
    for source in configured:
        if not isinstance(source, dict) or not {'repo', 'channel', 'folder'} <= source.keys():
            raise builder.BuildFailure('部署包的来源记录不完整')
        derived = make_source(source['repo'], source['channel'], source.get('branch', 'main'))
        if derived['folder'] != source['folder']:
            raise builder.BuildFailure('部署包的来源目录名无效')
    signature = lambda sources: {s['folder']: (s['repo'].casefold(), s['channel'], s.get('branch')) for s in sources}
    if len(signature(configured)) != len(configured) or (expected is not None and signature(configured) != signature(expected)):
        raise builder.BuildFailure('部署包的来源清单与生成配置不符')
    rows = by_folder(report)
    if not complete_report(report, configured) or signature(report['sources']) != signature(configured):
        raise builder.BuildFailure('部署包有未完成的来源')
    for source in configured:
        label = source['folder']
        row = rows[label]
        if not revision(row):
            raise builder.BuildFailure(f'{source["repo"]} 缺少有效的版本记录')
        directory = root/'mgu-repo'/label
        row['items'] = builder.inspect_repo(directory, source)
        if source['channel'] == 'catalog':
            catalog = builder.read_catalog(directory/'catalog.json', source['repo'])
            inventory = catalog_inventory(catalog)
        else:
            names = {path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file()}
            inventory = builder.tree_inventory(builder.read_json(directory/'mgu-repo.json'), names,
                                               lambda name: builder.read_json(directory/name))
            fingerprint_tree(inventory, names, lambda name: (directory/name).open('rb'))
        saved_inventory = row.get('inventory')
        if saved_inventory is not None and saved_inventory != inventory:
            raise builder.BuildFailure(f'{source["repo"]} 的组件指纹与部署包不符')
        row['inventory'] = inventory
        if report.get('mode') != 'delta' and source['repo'].casefold() == 'iteriodev/car-apps' and source['channel'] == 'tags' and 'iteriodev_seats' not in dict(row['items']):
            raise builder.BuildFailure('IterioDev 包缺少 Seats')
    checksum_file = root/'MGU-LIBRARY-SHA256.txt'
    if verify_hash_list and checksum_file.is_file():
        repo_root = (root/'mgu-repo').resolve()
        for line in checksum_file.read_text(encoding='utf-8').splitlines():
            digest, separator, name = line.partition('  ')
            parts = name.split('/')
            if not separator or not re.fullmatch(r'[a-f0-9]{64}', digest) or parts[0] != 'mgu-repo' or any(not builder.safe_part(p) for p in parts):
                raise builder.BuildFailure('文件校验清单格式有误')
            target = root.joinpath(*parts)
            if not target.resolve().is_relative_to(repo_root) or not target.is_file() or builder.sha256(target) != digest:
                raise builder.BuildFailure(f'文件校验失败 · {name}')
    return report


def import_library(path):
    if path.is_dir():
        return valid_report(path, verify_hash_list=True)
    with tempfile.TemporaryDirectory(prefix='mgu-import-') as tmp, zipfile.ZipFile(path) as z:
        root = Path(tmp)
        candidates = [n for n in z.namelist() if n.rsplit('/', 1)[-1] == REPORT and not n.startswith('__MACOSX/')]
        if len(candidates) != 1:
            raise builder.BuildFailure('ZIP 内须有唯一的版本报告。请选择生成后的完整部署包。')
        prefix = candidates[0][:-len(REPORT)]
        seen = set()
        total = 0
        for entry in z.infolist():
            if not entry.filename.startswith(prefix) or entry.is_dir():
                continue
            rel = entry.filename[len(prefix):]
            if rel not in REPORT_FILES and not rel.startswith('mgu-repo/'):
                continue
            parts = rel.split('/')
            if any(not builder.safe_part(part) for part in parts) or stat.S_ISLNK(entry.external_attr >> 16) or rel.casefold() in seen:
                raise builder.BuildFailure(f'ZIP 路径无效或重复 · {rel}')
            total += entry.file_size
            if total > 3_000_000_000 or len(seen) >= 150_000:
                raise builder.BuildFailure('ZIP 超出完整仓库的容量限制')
            seen.add(rel.casefold())
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(entry) as data, target.open('wb') as out:
                shutil.copyfileobj(data, out)
        return valid_report(root, verify_hash_list=True)


def list_usb_packages(directory):
    packages = []
    for path in directory.glob('*.zip'):
        try:
            with zipfile.ZipFile(path) as archive:
                member = archive.getinfo(REPORT)
                if member.file_size > 4_000_000:
                    continue
                report = json.loads(archive.read(member))
            rows = report['sources']
            if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
                continue
            cached = any('CACHED' in row.get('freshness', '') for row in rows)
            kind = '增量' if report.get('mode') == 'delta' else ('缓存' if cached else '全量')
            packages.append((path, str(report.get('generated_utc') or ''), len(rows), kind))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, zipfile.BadZipFile):
            continue
    return sorted(packages, key=lambda item: (item[1], item[0].name), reverse=True)


def write_zip(root, dest, emit):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.mgu-', suffix='.partial', dir=dest.parent, delete=False) as f:
        temp = Path(f.name)
    try:
        emit('正在压缩完整仓库…')
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        emit('正在复核 ZIP 可读性…')
        with zipfile.ZipFile(temp) as archive:
            bad = archive.testzip()
            if bad:
                raise builder.BuildFailure(f'ZIP 校验失败 · {bad}')
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)


def export_library(destination, cache, proxy, offline, hashes, fail_fast, emit, progress, sources, as_zip=True):
    def build_at(root):
        args = Namespace(usb=str(root), cache=str(cache), proxy=proxy, offline=offline, hashes=hashes, fail_fast=fail_fast)
        rc = builder.build(args, emit=emit, progress=progress, portable=ROOT, sources=sources)
        return rc, builder.read_json(root/REPORT)

    if not as_zip:
        rc, raw_report = build_at(destination)
        report = valid_report(destination, expected=sources) if complete_report(raw_report, sources) else raw_report
        return rc, report, True
    with tempfile.TemporaryDirectory(prefix='mgu-zip-') as temp:
        root = Path(temp)
        rc, report = build_at(root)
        if not complete_report(report, sources):
            return rc, report, False
        if rc and not offline:
            emit('本次未确认所有来源为最新。可使用「离线缓存导出」制作带缓存标记的完整包。')
            return rc, report, False
        report = valid_report(root, expected=sources)
        progress(len(sources), len(sources), '正在打包与校验')
        write_zip(root, destination, emit)
        return rc, report, True
