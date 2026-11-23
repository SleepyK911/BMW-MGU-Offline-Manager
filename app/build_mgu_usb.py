#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BMW MGU Store multi-source offline USB repository builder.

Always checks upstream revisions when online. Preserves the upstream's source
identity (mgu-copy.json), component IDs, versions and original bytes.
Uses Robert Nisipeanu's own mgu-install.js usb-repo implementation to generate
source trees and checked catalogs. No vehicle is modified by this script.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://api.github.com"
AGENT = "BMW-MGU-Offline-Library/2.0 (personal backup)"
WINDOW_FLAGS = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
# channel: branch -> latest main commit; tags -> latest stable X.Y.Z tag;
# catalog -> latest official published release catalog and each checked asset.
SOURCES = [
    {"repo": "robertnisipeanu/mgu-store",             "channel": "catalog", "folder": "00-mgu-store", "note": "MGU Store and core dependencies"},
    {"repo": "robertnisipeanu/idrive-apps",           "channel": "catalog", "folder": "01-idrive-apps", "note": "Waze on Cluster and cluster-render"},
    {"repo": "cauliflowermossgrape/idrive7-apps",    "channel": "branch",  "folder": "02-idrive7-apps", "note": "Redline, Warm-Up, M Vitals, tools"},
    {"repo": "bimmer-lab/apps",                      "channel": "branch",  "folder": "03-bimmer-lab", "note": "HUD Widgets and Cluster Themes"},
    {"repo": "eroobeeroo/idrive7-cluster-info",      "channel": "branch",  "folder": "04-cluster-info", "note": "Cluster Info, Layouts, Centre"},
    {"repo": "IterioDev/car-apps",                    "channel": "tags",    "folder": "05-iteriodev", "note": "Seats and iteriodev-comfort, other apps/tools"},
    {"repo": "JustAnotherPU/mgu-apps",               "channel": "catalog", "folder": "06-jap", "note": "iDrive Monitor and network tools"},
    {"repo": "hexlay/idrive7-custom-apps",          "channel": "branch",  "folder": "07-hexlay", "note": "Spotify and M-Copilot"},
    {"repo": "JackieZ123430/idrive7-apps-cn",       "channel": "branch",  "folder": "08-cn", "note": "Chinese translations; duplicate app IDs possible"},
]

class BuildFailure(RuntimeError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlparse(newurl).hostname != 'api.github.com':
            redirected.remove_header('Authorization')
        return redirected


def make_opener(proxy=None):
    handlers = [SafeRedirect()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'https': proxy, 'http': proxy}))
    return urllib.request.build_opener(*handlers)


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.partial')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def download_file(url: str, dest: Path, opener, *, expected_size=None, expected_hash=None,
                  attempts=3, force=False, max_bytes=250_000_000):
    if dest.is_file() and not force:
        if (expected_size is None or dest.stat().st_size == expected_size) and (
                expected_hash is None or sha256(dest) == expected_hash):
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        temp = dest.with_name(dest.name + '.partial')
        try:
            headers = {'User-Agent': AGENT, 'Accept': 'application/vnd.github+json' if
                       urllib.parse.urlparse(url).hostname == 'api.github.com' else '*/*',
                       'Cache-Control': 'no-cache'}
            token = os.getenv('GITHUB_TOKEN', '').strip()
            request = urllib.request.Request(url, headers=headers)
            if token and urllib.parse.urlparse(url).hostname == 'api.github.com':
                request.add_unredirected_header('Authorization', 'Bearer ' + token)
            with opener.open(request, timeout=90) as response, temp.open('wb') as f:
                length = 0
                while True:
                    block = response.read(512 * 1024)
                    if not block:
                        break
                    length += len(block)
                    if length > max_bytes:
                        raise BuildFailure(f'Download exceeds {max_bytes // 1000000} MB: {url}')
                    f.write(block)
            if expected_size is not None and temp.stat().st_size != expected_size:
                raise BuildFailure(f'Artifact length mismatch: expected {expected_size}, got {temp.stat().st_size}')
            if expected_hash is not None and sha256(temp) != expected_hash:
                raise BuildFailure(f'Artifact SHA256 mismatch: {dest.name}')
            temp.replace(dest)
            return dest
        except Exception as e:
            temp.unlink(missing_ok=True)
            if isinstance(e, urllib.error.HTTPError) and (
                    e.code in (404, 429) or (e.code == 403 and e.headers and
                    (e.headers.get('X-RateLimit-Remaining') == '0' or e.headers.get('Retry-After')))):
                raise BuildFailure(f'Download failed {url}: {e}') from e
            if attempt + 1 == attempts:
                raise BuildFailure(f'Download failed {url}: {e}') from e
            time.sleep(1 + attempt)


def get_json(url, opener, *, max_bytes=2_000_000):
    with tempfile.TemporaryDirectory(prefix='mgu-json-') as d:
        p = download_file(url, Path(d)/'response.json', opener, force=True,
                          max_bytes=max_bytes, attempts=2)
        return json.loads(p.read_text(encoding='utf-8'))


def github_refs(repo, opener):
    """Read the public smart-HTTP Git ref advertisement without the REST API quota."""
    request = urllib.request.Request(f'https://github.com/{repo}.git/info/refs?service=git-upload-pack',
                                     headers={'User-Agent': AGENT, 'Cache-Control': 'no-cache',
                                              'Git-Protocol': 'version=1'})
    with opener.open(request, timeout=30) as response:
        if response.headers.get_content_type() != 'application/x-git-upload-pack-advertisement':
            raise BuildFailure(f'GitHub did not return Git refs: {repo}')
        payload = response.read(8_000_001)
    if len(payload) > 8_000_000:
        raise BuildFailure(f'Git ref list too large: {repo}')
    refs = {}
    pos = 0
    while pos < len(payload):
        size = int(payload[pos:pos+4], 16)
        if size == 0:
            pos += 4
            continue
        if size < 4 or pos + size > len(payload):
            raise BuildFailure(f'Invalid Git ref response: {repo}')
        packet = payload[pos+4:pos+size].split(b'\0', 1)[0].rstrip(b'\n')
        match = re.fullmatch(rb'([a-f0-9]{40}) (HEAD|refs/[^\x00-\x20]+)', packet)
        if match:
            sha, ref = (part.decode('utf-8', errors='surrogateescape') for part in match.groups())
            refs[ref] = sha
        pos += size
    if not refs:
        raise BuildFailure(f'No Git refs received: {repo}')
    return refs


def stable_tag(refs, repo):
    matches = []
    for name, sha in refs:
        m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', name)
        if m:
            matches.append((tuple(int(x) for x in m.groups()), name, sha))
    if not matches:
        raise BuildFailure(f'No stable X.Y.Z tags: {repo}')
    _, tag, sha = max(matches)
    return tag, sha


def rest_ref(source, opener):
    repo = source['repo']
    if source['channel'] == 'branch':
        tag = source.get('branch', 'main')
        ref = 'HEAD' if tag == 'HEAD' else 'heads/' + urllib.parse.quote(tag, safe='')
        return tag, get_json(f'{BASE}/repos/{repo}/commits/{ref}', opener)['sha']
    if source['channel'] == 'tags':
        matches = []
        for page in range(1, 101):
            suffix = '' if page == 1 else f'&page={page}'
            tags = get_json(f'{BASE}/repos/{repo}/tags?per_page=100{suffix}', opener)
            for entry in tags:
                matches.append((entry['name'], entry['commit']['sha']))
            if len(tags) < 100:
                break
        else:
            raise BuildFailure(f'Could not finish scanning tags: {repo}')
        return stable_tag(matches, repo)
    raise BuildFailure('Not an archive source: ' + repo)


def resolve_ref(source, opener):
    repo, channel = source['repo'], source['channel']
    try:
        refs = github_refs(repo, opener)
    except (OSError, ValueError, BuildFailure) as ex:
        try:
            tag, sha = rest_ref(source, opener)
        except BuildFailure as fallback:
            raise BuildFailure(f'Git refs: {ex}; GitHub API: {fallback}') from fallback
    else:
        if channel == 'branch':
            tag = source.get('branch', 'main')
            sha = refs.get('HEAD' if tag == 'HEAD' else f'refs/heads/{tag}')
            if not sha:
                raise BuildFailure(f'GitHub branch not found: {repo} {tag}')
        elif channel == 'tags':
            prefix = 'refs/tags/'
            tags = ((ref[len(prefix):], refs.get(ref + '^{}', sha)) for ref, sha in refs.items() if ref.startswith(prefix))
            tag, sha = stable_tag(tags, repo)
        else:
            raise BuildFailure('Not an archive source: ' + repo)
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise BuildFailure(f'Invalid GitHub commit for {repo}')
    return {'repo': repo, 'channel': channel, 'sha': sha, 'tag': tag,
            'checked_utc': dt.datetime.now(dt.timezone.utc).isoformat()}


def upstream_ref(source, opener, cache: Path, offline=False, emit=print):
    """Read current branch/tag SHA every run; only cached SHA for offline fallback."""
    repo, channel, label = source['repo'], source['channel'], source['folder']
    meta_file = cache / 'refs' / (label + '.json')
    old = read_json(meta_file, {})
    if offline:
        if not old.get('sha'):
            raise BuildFailure(f'No cached ref for {repo}')
        return old, 'CACHED (offline; latest unknown)'
    try:
        return resolve_ref(source, opener), 'LATEST CONFIRMED'
    except Exception as e:
        if old.get('sha'):
            emit(f'  WARNING: Cannot refresh {repo}: {e}. Using cached {old["sha"][:12]}.')
            return old, 'CACHED (network failed; latest unknown)'
        raise


def unpack_zip(archive: Path, target: Path):
    """Safe expansion of a GitHub source archive, not a binary package."""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        if len(members) > 20000:
            raise BuildFailure('Too many ZIP entries')
        roots = {p.filename.split('/')[0] for p in members if p.filename and not p.filename.startswith('__MACOSX/')}
        if len(roots) != 1:
            raise BuildFailure('Unexpected ZIP root: ' + str(roots))
        prefix = next(iter(roots)) + '/'
        total = 0
        seen = set()
        for entry in members:
            name = entry.filename.replace('\\', '/')
            if entry.is_dir() or name.startswith('__MACOSX/'):
                continue
            if not name.startswith(prefix):
                raise BuildFailure('Unsafe ZIP entry outside root')
            rel = name[len(prefix):]
            parts = Path(rel).parts
            if not parts or any(not safe_part(part) for part in rel.split('/')):
                raise BuildFailure('Unsafe ZIP path')
            if rel.casefold() in seen:
                raise BuildFailure('Duplicate ZIP path: ' + rel)
            seen.add(rel.casefold())
            total += entry.file_size
            if total > 200_000_000:
                raise BuildFailure('Unpacked source exceeds 200 MB MGU Store limit')
            # GitHub repositories contain only original files; omit symbolic links.
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            path = target.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with z.open(entry) as src, path.open('wb') as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    if not (target / 'mgu-repo.json').is_file():
        raise BuildFailure('Source does not contain mgu-repo.json')


def safe_part(part):
    return bool(part and part not in ('.', '..') and not part.endswith(('.', ' '))
                and not re.search(r'[<>:"/\\|?*\x00-\x1f]', part)
                and not re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])', part.split('.')[0]))


def source_archive(source, ref, opener, cache, offline, emit=print):
    repo, label, sha = source['repo'], source['folder'], ref['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise BuildFailure(f'Invalid cached commit: {repo}')
    archive = cache / 'archives' / label / (sha + '.zip')
    if not archive.exists():
        if offline:
            raise BuildFailure(f'Archive not cached: {repo} {sha}')
        url = f'https://codeload.github.com/{repo}/zip/{sha}'
        emit(f'  Fetching code at {sha[:12]} ({repo})')
        download_file(url, archive, opener, max_bytes=200_000_000)
    return archive


def prepare_branch(source, opener, cache, scratch, offline, emit=print, pinned=None):
    ref, freshness = (pinned[0], 'LATEST CONFIRMED') if pinned else upstream_ref(source, opener, cache, offline, emit)
    label, sha = source['folder'], ref['sha']
    archive = source_archive(source, ref, opener, cache, offline, emit)
    checkout = scratch / label
    try:
        unpack_zip(archive, checkout)
    except (zipfile.BadZipFile, EOFError):
        if not offline:
            archive.unlink(missing_ok=True)
        raise
    if pinned:
        manifest = checkout/'mgu-repo.json'
        doc = read_json(manifest)
        doc.pop('apps', None)
        doc.pop('tools', None)
        doc['components'] = pinned[1]
        write_json(manifest, doc)
    info = {'commit': sha, 'tag': ref['tag'], 'freshness': freshness,
            'archive_sha256': sha256(archive), 'ref_checked_utc': ref.get('checked_utc')}
    return checkout, info


def fetch_catalog(source, opener, destination, attempts=3):
    repo = source['repo']
    url = f'https://github.com/{repo}/releases/download/catalog/catalog.json'
    download_file(url, destination, opener, force=True, max_bytes=4_000_000, attempts=attempts)
    return read_catalog(destination, repo)


def read_catalog(path, repo):
    catalog = read_json(path, {})
    if catalog.get('schema') not in (1, 2) or not catalog.get('components') or not isinstance(catalog['components'], list):
        raise BuildFailure(f'Invalid or empty catalog for {repo}')
    seen = set()
    for entry in catalog['components']:
        artifact = entry.get('artifact', {})
        ident, ver = entry.get('id'), entry.get('version')
        name = artifact.get('name')
        size = artifact.get('size')
        if (not isinstance(ident, str) or not safe_part(ident) or ident.casefold() in seen or
                not isinstance(ver, str) or not ver or not isinstance(name, str) or not safe_part(name) or
                name != f'{ident}@{ver}.tgz' or not re.fullmatch(r'[a-f0-9]{64}', str(artifact.get('sha256'))) or
                type(size) is not int or size <= 0):
            raise BuildFailure('Invalid catalog component identity or checksum: ' + repr(name))
        seen.add(ident.casefold())
    return catalog


def prepare_catalog(source, opener, cache, scratch, offline, emit=print, pinned=None):
    repo, label = source['repo'], source['folder']
    catalog_file = cache / 'catalogs' / label / 'catalog.json'
    fetched = scratch / 'upstream.json'
    freshness = 'LATEST CONFIRMED'
    if pinned:
        catalog_file, selected = pinned
        original = read_catalog(catalog_file, repo)
    elif not offline:
        try:
            original = fetch_catalog(source, opener, fetched)
            catalog_file = fetched
        except Exception as e:
            if catalog_file.exists():
                freshness = 'CACHED (network failed; latest unknown)'
                emit(f'  WARNING: Cannot refresh {repo} catalog: {e}; using last catalog.')
                original = read_catalog(catalog_file, repo)
            else:
                raise
    else:
        if not catalog_file.exists():
            raise BuildFailure(f'No cached release catalog: {repo}')
        freshness = 'CACHED (offline; latest unknown)'
        original = read_catalog(catalog_file, repo)
    stage = scratch / label
    stage.mkdir(parents=True)
    modified = copy.deepcopy(original)
    if pinned:
        modified['components'] = [entry for entry in modified['components'] if entry['id'] in selected]
    versions = {}
    for entry in modified['components']:
        artifact = entry.get('artifact', {})
        name = artifact.get('name')
        ver = entry.get('version')
        digest = artifact.get('sha256')
        size = artifact.get('size')
        raw = artifact.get('url') or name
        origin_url = urllib.parse.urljoin(f'https://github.com/{repo}/releases/download/catalog/', raw)
        cached = cache / 'artifacts' / digest[:2] / (digest + '.tgz')
        if not cached.exists() or cached.stat().st_size != size or sha256(cached) != digest:
            if offline:
                raise BuildFailure(f'Uncached artifact: {repo} {name}')
            emit(f'  Fetching {entry["id"]} {ver} ({size // 1024} KiB)')
            download_file(origin_url, cached, opener, expected_size=size, expected_hash=digest, max_bytes=250_000_000)
        shutil.copy2(cached, stage / name)
        artifact['url'] = name  # staged path only; original artifact bytes/metadata stay unchanged
        versions[entry['id']] = ver
        icon = entry.get('icon')
        if icon:
            icon_raw = urllib.parse.urljoin(f'https://github.com/{repo}/releases/download/catalog/', str(icon))
            iconname = entry['id'] + '.png'
            iconhash = entry.get('iconSha256')
            try:
                iconcache = cache / 'icons' / label / iconname
                if not iconcache.is_file() or (iconhash and sha256(iconcache) != iconhash):
                    if offline:
                        raise BuildFailure('Icon not cached')
                    download_file(icon_raw, iconcache, opener, max_bytes=4_000_000, force=True,
                                  expected_hash=iconhash if isinstance(iconhash, str) else None)
                shutil.copy2(iconcache, stage/iconname)
                entry['icon'] = iconname
            except Exception as ex:
                emit(f'  Icon skipped: {icon}: {ex}')
                entry.pop('icon', None)
                entry.pop('iconSha256', None)
    write_json(stage/'catalog.json', modified)
    return stage, {'commit': original.get('commit'), 'freshness': freshness,
                   'catalog_sha256': sha256(catalog_file), 'components': versions}, catalog_file


def tree_inventory(manifest, names, read):
    if manifest.get('schema') != 1:
        raise BuildFailure('Source manifest is not schema 1')
    entries = list(manifest.get('components') or [])
    entries.extend(dict(tool, kind='tool') for tool in manifest.get('tools', []))
    patterns = manifest.get('apps') or []
    for spec in patterns if isinstance(patterns, list) else [patterns]:
        pattern = spec.get('path', 'apps/*')
        if not re.fullmatch(r'(?:[^*{}]+/)?\*', pattern):
            raise BuildFailure(f'Unsupported app pattern: {pattern}')
        base = pattern[:-1]
        app_manifests = {spec['manifest']} if spec.get('manifest') else {'app.json', 'package.json'}
        folders = set()
        for name in names:
            if name.startswith(base):
                parts = name[len(base):].split('/')
                if len(parts) == 2 and parts[1] in app_manifests:
                    folders.add(parts[0])
        excluded = spec.get('exclude') or []
        fields = {k: v for k, v in spec.items() if k != 'exclude'}
        entries.extend(dict(fields, id=name, kind='app', path=base + name) for name in sorted(folders)
                       if name not in excluded and base + name not in excluded)

    items = {}
    for entry in entries:
        ident, kind, folder = (entry.get(key) for key in ('id', 'kind', 'path'))
        if not isinstance(ident, str) or ident in items or kind not in ('app', 'tool') or not safe_relative(folder):
            continue
        script = entry.get('build')
        service = kind == 'tool' and ('service' in entry or 'preload' in entry)
        if kind == 'tool':
            candidates = ['component.json']
        elif entry.get('manifest'):
            candidates = [entry['manifest']]
        else:
            candidates = ['app.json', 'package.json'] if isinstance(script, dict) else ['mgu-app.json']
        manifest_path = next((f'{folder}/{file}' for file in candidates if f'{folder}/{file}' in names), None)
        meta = entry if service else read(manifest_path) if manifest_path else {}
        version = meta.get('version')
        if not version or (meta.get('id') is not None and meta['id'] != ident):
            continue
        requires = dict(entry.get('requires', meta.get('requires')) or {})
        provides = entry.get('provides', meta.get('provides')) or entry.get('mguProvides', meta.get('mguProvides'))
        needs = entry.get('needs', meta.get('needs')) or entry.get('mguNeeds', meta.get('mguNeeds')) or []
        paths = [folder]
        if isinstance(script, dict):
            shared = script.get('shared') or []
            paths = [folder, *shared, *(script[k] for k in ('python', 'node') if script.get(k))] if shared else ['']
            if script.get('python'):
                requires.setdefault('mgu-python', '1.0.2')
            if script.get('sdk'):
                requires.setdefault('mgu-deploy', '0.3.2')
        if kind == 'tool' and provides:
            requires.setdefault('mgu-deploy', '0.4.0')
        elif kind == 'tool' and (entry.get('preload') or meta.get('dropins')):
            requires.setdefault('mgu-deploy', '0.3.2')
        if service:
            runtime = (entry.get('service') or {}).get('exec') or (entry.get('preload') or {}).get('library')
            if runtime:
                paths = [f'{folder}/{runtime}']
        items[ident] = {'version': str(version), 'entry': entry, 'paths': paths, 'requires': requires,
                        'provides': provides, 'needs': needs}
    if not items:
        raise BuildFailure('No apps or tools in source manifest')
    return items


def safe_relative(path):
    return isinstance(path, str) and all(safe_part(part) for part in path.split('/'))


def tree_items(manifest, names, read):
    return sorted((ident, component['version']) for ident, component in tree_inventory(manifest, names, read).items())


def inspect_repo(source_dir: Path, source):
    if read_json(source_dir/'mgu-copy.json') != {'copyOf': 'github:' + source['repo']}:
        raise BuildFailure('Missing or invalid mgu-copy.json: ' + source['repo'])
    if source['channel'] == 'catalog':
        catalog = read_catalog(source_dir/'catalog.json', source['repo'])
        if catalog['schema'] != 2:
            raise BuildFailure('USB catalog is not schema 2')
        for entry in catalog['components']:
            artifact = entry['artifact']
            if artifact.get('url') != artifact['name']:
                raise BuildFailure('USB catalog artifact is not a local file')
            path = source_dir / artifact['name']
            if not path.is_file() or path.stat().st_size != artifact['size'] or sha256(path) != artifact['sha256']:
                raise BuildFailure('Missing or corrupted output artifact: ' + path.name)
        return [(entry['id'], entry['version']) for entry in catalog['components']]
    names = {p.relative_to(source_dir).as_posix() for p in source_dir.rglob('*') if p.is_file()}
    return tree_items(read_json(source_dir/'mgu-repo.json', {}), names, lambda name: read_json(source_dir/name))


def atomic_replace_directory(new: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = dest.parent / ('.' + dest.name + '.backup')
    if backup.is_dir() and not dest.exists():
        backup.rename(dest)  # recover interrupted previous run
    if backup.exists():
        shutil.rmtree(backup)
    # copy onto target filesystem before switching so old good data remains intact
    pending = dest.parent / ('.' + dest.name + '.incoming')
    if pending.exists():
        shutil.rmtree(pending)
    shutil.copytree(new, pending)
    try:
        if dest.exists():
            dest.rename(backup)
        pending.rename(dest)
    except BaseException:
        if backup.exists() and not dest.exists():
            backup.rename(dest)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def get_mgu_installer(opener, cache, offline):
    path = cache / 'mgu-install.js'
    if not offline:
        try:
            download_file('https://raw.githubusercontent.com/robertnisipeanu/mgu-store/main/deploy/mgu-install.js',
                          path, opener, force=True, max_bytes=1_000_000)
        except Exception as e:
            if not path.exists():
                raise
            print('WARNING: Could not refresh official mgu-install.js; using last cached:', e)
    if not path.exists() or 'usb-repo' not in path.read_text(encoding='utf-8'):
        raise BuildFailure('Official MGU installer script missing/invalid')
    return path


def node_version(portable=None):
    exe = str(portable) if portable and portable.is_file() else shutil.which('node')
    if not exe:
        raise BuildFailure('Node.js 18+ is required (https://nodejs.org/)')
    env = os.environ.copy()
    env.pop('NODE_OPTIONS', None)
    out = subprocess.check_output([exe, '--version'], text=True, env=env, **WINDOW_FLAGS).strip()
    if int(out.lstrip('v').split('.')[0]) < 18:
        raise BuildFailure('Node.js >=18 required; installed: ' + out)
    return exe, out


def build(args, emit=print, progress=None, portable=None, sources=None, pinned=None):
    sources = tuple(SOURCES if sources is None else sources)
    if not sources:
        raise BuildFailure('请先添加至少一个 GitHub 来源')
    usb = Path(args.usb).expanduser().resolve()
    cache = Path(args.cache).expanduser().resolve()
    if usb == ROOT or usb == cache or usb.is_relative_to(cache) or cache.is_relative_to(usb/'mgu-repo'):
        raise BuildFailure('USB output must not overlap the script or its cache')
    usb.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    node, ver = node_version(portable/'node.exe' if portable else None)
    emit(f'Node {ver} | USB target: {usb} | cache: {cache}')
    opener = make_opener(args.proxy)
    installer = portable/'mgu-install.js' if portable else get_mgu_installer(opener, cache, args.offline)
    if not installer.is_file():
        raise BuildFailure('Portable mgu-install.js is missing')
    results = []
    output_root = usb / 'mgu-repo'
    output_root.mkdir(exist_ok=True)
    for index, source in enumerate(sources, 1):
        label = source['folder']
        if progress:
            progress(index - 1, len(sources), source['repo'])
        emit(f'\n[{index}/{len(sources)}] {source["repo"]} [{source["channel"]}]')
        try:
            snapshot = (pinned or {}).get(label)
            with tempfile.TemporaryDirectory(prefix='mgu-build-') as t:
                scratch = Path(t)
                if source['channel'] == 'catalog':
                    inp, details, fetched_catalog = prepare_catalog(source, opener, cache, scratch/'inputs', args.offline, emit, snapshot)
                else:
                    inp, details = prepare_branch(source, opener, cache, scratch/'inputs', args.offline, emit, snapshot)
                target = scratch/'output'
                target.mkdir()
                cmd = [node, str(installer), 'usb-repo', str(target), str(inp)]
                env = os.environ.copy()
                env.pop('NODE_OPTIONS', None)
                process = subprocess.run(cmd, timeout=900, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding='utf-8', errors='replace', env=env, **WINDOW_FLAGS)
                emit(process.stdout.rstrip())
                if process.returncode:
                    raise BuildFailure(process.stdout.strip() or f'MGU usb-repo exited {process.returncode}')
                created = target / 'mgu-repo' / label
                if not created.is_dir():
                    raise BuildFailure('Official builder did not create expected source folder: ' + str(created))
                write_json(created/'mgu-copy.json', {'copyOf': 'github:' + source['repo']})
                items = inspect_repo(created, source)
                if not snapshot and source['repo'].casefold() == 'iteriodev/car-apps' and source['channel'] == 'tags' and 'iteriodev_seats' not in dict(items):
                    raise BuildFailure('Stable IterioDev tag contains no Seats; refusing to replace known-good USB copy')
                if details['freshness'] == 'LATEST CONFIRMED':
                    if source['channel'] == 'catalog':
                        saved = cache/'catalogs'/label/'catalog.json'
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        pending = saved.with_suffix('.partial')
                        shutil.copyfile(fetched_catalog, pending)
                        pending.replace(saved)
                    else:
                        write_json(cache/'refs'/(label + '.json'), {'repo': source['repo'], 'channel': source['channel'],
                                   'sha': details['commit'], 'tag': details['tag'], 'checked_utc': details['ref_checked_utc']})
                atomic_replace_directory(created, output_root/label)
                result = dict(source, status='OK', included=len(items), items=items, **details)
                results.append(result)
                emit(f'  SUCCESS: {len(items)} listed apps/tools. Origin: github:{source["repo"]}')
        except Exception as e:
            existing = output_root / label
            state = 'OLD USB COPY PRESERVED' if existing.exists() else 'MISSING'
            results.append(dict(source, status=state, error=str(e)))
            emit(f'  ERROR: {e}\n  STATUS: {state}')
            if args.fail_fast:
                break
    # Tool scripts, caches and report are NOT copied into mgu-repo, so Store ignores them.
    report = {'generated_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'builder': '2.0',
              'note': 'USB is a repository, NOT an automatic bulk-install command',
              'configured_sources': list(sources), 'sources': results}
    write_json(usb/'MGU-LIBRARY-STATUS.json', report)
    lines = ['BMW MGU Store offline library | ' + report['generated_utc'],
             'Copies preserve online update identity via mgu-copy.json.',
             'Offline USB: install or update only the apps you choose in MGU Store > USB.', '',
             'Source | State | Count | Version / SHA']
    for r in results:
        lines.append(f"{r['repo']} | {r['status']} | {r.get('included', '?')} | "
                     f"{r.get('tag', r.get('commit', r.get('catalog_sha256', '?')))}")
    lines += ['', 'An OK source marked CACHED has NOT been confirmed latest.',
              'Do not install all by default. Seats is untested on ID7 real vehicles.',
              'Do not install the same app ID from original and Chinese translation sources.']
    (usb/'MGU-LIBRARY-STATUS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if args.hashes:
        with (usb/'MGU-LIBRARY-SHA256.txt').open('w', encoding='utf-8') as out:
            for p in sorted(output_root.rglob('*')):
                if p.is_file() and not p.relative_to(output_root).parts[0].startswith('.'):
                    out.write(f'{sha256(p)}  {p.relative_to(usb).as_posix()}\n')
    else:
        (usb/'MGU-LIBRARY-SHA256.txt').unlink(missing_ok=True)
    errors = [r for r in results if r['status'] != 'OK']
    stale = [r for r in results if r['status'] == 'OK' and 'CACHED' in r.get('freshness', '')]
    emit(f'\nSource success: {len(results)-len(errors)} / {len(sources)} | old/missing: {len(sources)-len(results)+len(errors)} | not confirmed latest: {len(stale)}')
    emit(f'USB folder: {output_root}')
    emit('On car: MGU Store -> USB -> choose an app -> Install/Update')
    if errors or stale:
        emit('Some sources are NOT CONFIRMED CURRENT. See MGU-LIBRARY-STATUS.txt.')
    return 0 if not errors and not stale else 2


def main():
    p = argparse.ArgumentParser(description='Build a full, per-app selectable BMW MGU Store offline USB library')
    p.add_argument('--usb', required=True, help='USB root or a folder to copy to USB (e.g. E:\\ or /Volumes/MGU)')
    p.add_argument('--cache', default=str(ROOT/'cache'), help='Persistent source/archive/download cache on the COMPUTER')
    p.add_argument('--proxy', help='Optional HTTP proxy for GitHub download, e.g. http://127.0.0.1:7890')
    p.add_argument('--offline', action='store_true', help='Use only previously cached refs, code and published artifacts')
    p.add_argument('--fail-fast', action='store_true', help='Stop at the first failed source, keeping old output')
    p.add_argument('--hashes', action='store_true', help='Generate optional SHA256 list of USB files (slower)')
    args = p.parse_args()
    try:
        return build(args)
    except (BuildFailure, OSError, subprocess.CalledProcessError) as ex:
        print('FATAL:', ex, file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
