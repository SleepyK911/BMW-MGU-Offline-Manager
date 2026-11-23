"""Plan and build Store USB packages against a user-confirmed deployment record."""
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
import re
import tempfile
import zipfile

import build_mgu_usb as builder
import library as lib


def version_key(version):
    return tuple(int(part) for part in version.split('.')) if re.fullmatch(r'\d+\.\d+\.\d+', version or '') else None


def updated(current, installed):
    if not installed:
        return True
    new_version, old_version = (item['version'] for item in (current, installed))
    new, old = version_key(new_version), version_key(old_version)
    if new and old and new < old:
        return False
    return new_version != old_version or bool(installed.get('hash') and current['hash'] != installed['hash'])


def deployed_after(previous, report, selected=None):
    records = lib.by_folder(previous)
    if report.get('mode') == 'delta' and not records:
        raise builder.BuildFailure('请先记录一个全量部署包。')
    for row in report['sources']:
        label = row['folder']
        record = dict(records.get(label, {}))
        inventory = dict(record.get('inventory', {}))
        for ident, component in row['inventory'].items():
            if selected is None or (label, ident) in selected:
                inventory[ident] = {k: component[k] for k in ('version', 'hash', 'provides')}
        record.update({k: row[k] for k in ('repo', 'folder', 'channel')}, inventory=inventory)
        if report.get('mode') != 'delta' and row['channel'] != 'catalog':
            record.update(reference=row['inventory'], reference_commit=row['commit'])
        if inventory or record.get('reference'):
            records[label] = record
    return {'recorded_utc': lib.now(), 'sources': list(records.values())}


def apply_manual_versions(deployed, manual, sources):
    records = {key: dict(row, inventory=dict(row['inventory'])) for key, row in lib.by_folder(deployed).items()}
    for source in sources:
        label = source['folder']
        for ident, version in manual.get(label, {}).items():
            record = records.setdefault(label, dict(source, inventory={}))
            old = record['inventory'].get(ident, {})
            if version != old.get('version'):
                record['inventory'][ident] = {'version': version, 'hash': None, 'provides': None}
    return dict(deployed, sources=list(records.values()))


def select_updates(inventory, deployed, emit):
    previous = lib.by_folder(deployed)
    installed = {(label, ident): item for label, row in previous.items() for ident, item in row['inventory'].items()}
    all_items = {(label, ident): item for label, items in inventory.items() for ident, item in items.items()}
    changes = {key for key, item in all_items.items() if updated(item, installed.get(key))}
    selected = set(changes)
    pending = deque(sorted(changes))

    def add_dependency(key):
        if key not in selected:
            selected.add(key)
            pending.append(key)
            emit(f'  补充依赖 {key[1]} {all_items[key]["version"]}')

    def sufficient(item, minimum):
        return (version_key(item['version']) or ()) >= minimum

    while pending:
        label, ident = pending.popleft()
        item = all_items[label, ident]
        for dep, version in item['requires'].items():
            minimum = version_key(version)
            if minimum is None:
                raise builder.BuildFailure(f'{ident} 的依赖版本无效 · {dep} {version}')
            if any(k[1] == dep and sufficient(record, minimum) for k, record in installed.items()):
                continue
            options = [k for k, candidate in all_items.items() if k[1] == dep and sufficient(candidate, minimum)]
            if not options:
                emit(f'  未找到依赖 {dep} ≥ {version}（{ident}）。请在车机核对已有依赖或补充来源。')
                continue
            best = max(options, key=lambda k: (k[0] == label, version_key(all_items[k]['version']) or ()))
            add_dependency(best)
        needs = item.get('needs') or []
        for capability in [needs] if isinstance(needs, str) else needs:
            if any(record.get('provides') == capability for record in installed.values()):
                continue
            providers = [k for k, candidate in all_items.items() if candidate.get('provides') == capability]
            if providers:
                add_dependency(max(providers, key=lambda k: (k[0] == label, version_key(all_items[k]['version']) or ())))
            else:
                emit(f'  未找到能力提供者 {capability}（{ident}）。请在车机核对。')
    grouped = defaultdict(set)
    for label, ident in selected:
        grouped[label].add(ident)
    return grouped, changes


def export_delta(destination, cache, proxy, hashes, sources, deployed, emit, progress):
    if not lib.by_folder(deployed):
        raise builder.BuildFailure('请先在「USB 部署包」记录已部署的全量包。')
    opener = builder.make_opener(proxy)
    with tempfile.TemporaryDirectory(prefix='mgu-delta-') as temp:
        scratch = Path(temp)
        inventory, upstream = {}, {}
        recorded = lib.by_folder(deployed)
        for index, source in enumerate(sources, 1):
            label = source['folder']
            progress(index - 1, len(sources), source['repo'])
            emit(f'[{index}/{len(sources)}] 核对 {source["repo"]}')
            if source['channel'] == 'catalog':
                path = scratch/(label + '.json')
                catalog = builder.fetch_catalog(source, opener, path)
                upstream[label] = path
                inventory[label] = lib.catalog_inventory(catalog)
            else:
                ref = builder.resolve_ref(source, opener)
                upstream[label] = ref
                baseline = recorded.get(label, {})
                if baseline.get('reference_commit') == ref['sha'] and baseline.get('reference'):
                    inventory[label] = baseline['reference']
                    continue
                archive = builder.source_archive(source, ref, opener, cache, False, emit)
                try:
                    inventory[label] = lib.archive_inventory(archive, fingerprints=True)
                except (zipfile.BadZipFile, EOFError):
                    archive.unlink(missing_ok=True)
                    raise

        selected, changes = select_updates(inventory, deployed, emit)
        if not changes:
            emit('当前来源没有待部署的更新。')
            return None
        planned = [source for source in sources if source['folder'] in selected]
        pinned = {}
        for source in planned:
            label = source['folder']
            ids = sorted(selected[label])
            emit(f'{source["repo"]} · ' + ', '.join(f'{ident} {inventory[label][ident]["version"]}' for ident in ids))
            if source['channel'] == 'catalog':
                pinned[label] = (upstream[label], ids)
            else:
                pinned[label] = (upstream[label], [inventory[label][ident]['entry'] for ident in ids])

        root = scratch/'usb'
        args = SimpleNamespace(usb=root, cache=cache, proxy=proxy, offline=False, hashes=hashes, fail_fast=True)
        rc = builder.build(args, emit=emit, progress=progress, portable=lib.ROOT, sources=planned, pinned=pinned)
        report = builder.read_json(root/lib.REPORT)
        if rc or not lib.complete_report(report, planned):
            raise builder.BuildFailure('增量包未生成。请在「构建输出」查看失败来源。')
        report.update(mode='delta', comparison_utc=deployed['recorded_utc'], updated_count=len(changes))
        for row in report['sources']:
            label = row['folder']
            if set(dict(row['items'])) != selected[label]:
                raise builder.BuildFailure(f'{row["repo"]} 导出组件与增量清单不符。')
            row['updated_ids'] = sorted(ident for ident in selected[label] if (label, ident) in changes)
        builder.write_json(root/lib.REPORT, report)
        text_report = root/'MGU-LIBRARY-STATUS.txt'
        with text_report.open('a', encoding='utf-8') as out:
            out.write('\nIncremental deployment package\n')
            for row in report['sources']:
                for ident, version in row['items']:
                    reason = 'Update' if ident in row['updated_ids'] else 'Dependency'
                    out.write(f'{row["repo"]} | {ident} {version} | {reason}\n')
        report = lib.valid_report(root, expected=planned)
        # ZIP readers can verify this summary against the packaged manifests and files.
        builder.write_json(root/lib.REPORT, report)
        progress(len(planned), len(planned), '正在打包与校验')
        lib.write_zip(root, destination, emit)
        return report
