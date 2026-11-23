"""Windows desktop interface for the BMW MGU offline repository builder."""
from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import uuid
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import build_mgu_usb as builder
import incremental as delta
import library as lib

BG = '#f0f4f8'
WHITE = '#ffffff'
NAVY = '#14283e'
INK = '#1c2d41'
MUTED = '#4c6378'
BLUE = '#1761bc'
LINE = '#dce5ee'
VERSION = '1.2.1'


def local_time(value):
    if not value:
        return '—'
    try:
        date = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.astimezone().strftime('%Y-%m-%d %H:%M')
    except (AttributeError, ValueError):
        return '—'


class Manager:
    def __init__(self, root, data, usb_dir):
        self.root, self.data = root, data
        self.usb_dir = usb_dir
        self.state_path = data/'manager.json'
        self.state = builder.read_json(self.state_path, {})
        self.state.pop('history', None)
        self.active_sources = {s['folder']: s for s in self.state.get('sources', builder.SOURCES)}
        self.events = queue.Queue()
        self.busy = False
        self.controls = []
        self.status = tk.StringVar(value='就绪')
        root.title(f'BMW MGU 离线仓库管理器  {VERSION}')
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f'{min(1260, screen_width - 48)}x{min(900, screen_height - 95)}')
        root.minsize(940, 680)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.setup_styles()
        self.create_widgets()
        self.render_sources()
        self.refresh_packages()
        root.after(70, self.poll)

    def setup_styles(self):
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
            tkfont.nametofont(name).configure(family='Microsoft YaHei UI', size=10)
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), foreground=INK, background=BG)
        style.configure('TFrame', background=BG)
        style.configure('TButton', background=WHITE, padding=(11, 6), bordercolor=LINE)
        style.map('TButton', background=[('active', '#e3eefc'), ('disabled', '#e8edf3')])
        style.configure('Primary.TButton', background=BLUE, foreground=WHITE, bordercolor=BLUE,
                        padding=(16, 7), font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Primary.TButton', background=[('disabled', '#e8edf3'), ('active', '#0e4e9d')],
                  foreground=[('disabled', MUTED)])
        style.configure('Treeview', rowheight=27, background=WHITE, fieldbackground=WHITE, bordercolor=LINE)
        style.configure('Treeview.Heading', background='#e6eef7', padding=(7, 7), font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('Treeview', background=[('selected', '#cce3ff')], foreground=[('selected', INK)])
        style.configure('TNotebook', background=BG, bordercolor=LINE)
        style.configure('TNotebook.Tab', padding=(15, 6), background='#e4ecf5')
        style.map('TNotebook.Tab', background=[('selected', WHITE)])
        style.configure('Horizontal.TProgressbar', background=BLUE, troughcolor='#dce6f2', bordercolor=LINE)

    def button(self, parent, name, action, primary=False, register=True):
        btn = ttk.Button(parent, text=name, command=action, style='Primary.TButton' if primary else 'TButton')
        if register:
            self.controls.append(btn)
        return btn

    def create_widgets(self):
        header = tk.Frame(self.root, bg=NAVY, padx=24, pady=16)
        header.pack(fill='x')
        tk.Label(header, text='BMW MGU 离线仓库', bg=NAVY, fg=WHITE,
                 font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        tk.Label(header, text='比对 GitHub 版本，导出 MGU Store USB 全量仓库', bg=NAVY, fg='#c7d7e8',
                 font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(3, 0))

        body = ttk.Frame(self.root, padding=(23, 13, 23, 11))
        body.pack(fill='both', expand=True)
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(0, 9))
        self.button(actions, '生成最新版 ZIP', lambda: self.export(), True).pack(side='left')
        self.button(actions, '检查在线版本', self.check).pack(side='left', padx=7)
        self.button(actions, '仅下载更新', self.export_updates).pack(side='left', padx=(0, 7))
        self.more = ttk.Menubutton(actions, text='导入与其他导出 ▾')
        self.more.pack(side='left', padx=(0, 7))
        self.controls.append(self.more)
        menu = tk.Menu(self.more, tearoff=False)
        menu.add_command(label='导入已有部署 ZIP', command=lambda: self.import_snapshot(False))
        menu.add_command(label='导入已有 USB / 文件夹', command=lambda: self.import_snapshot(True))
        menu.add_separator()
        menu.add_command(label='离线缓存导出 ZIP', command=lambda: self.export(offline=True))
        menu.add_command(label='写入 USB / 文件夹', command=lambda: self.export(as_zip=False))
        menu.add_command(label='从缓存写入 USB / 文件夹', command=lambda: self.export(offline=True, as_zip=False))
        self.more['menu'] = menu
        self.button(actions, '设置', self.settings).pack(side='right')
        self.button(actions, '打开 USB 文件夹', self.open_output, register=False).pack(side='right', padx=7)

        self.summary = ttk.Label(body, foreground=MUTED)
        self.summary.pack(fill='x', pady=(0, 8))
        source_actions = ttk.Frame(body)
        source_actions.pack(fill='x', pady=(0, 7))
        self.button(source_actions, '添加 GitHub 来源', self.add_source).pack(side='left')
        self.button(source_actions, '删除选中来源', self.remove_source).pack(side='left', padx=7)
        self.source_count = ttk.Label(source_actions, foreground=MUTED)
        self.source_count.pack(side='right')
        source_frame = ttk.Frame(body)
        source_frame.pack(fill='x')
        cols = ('repo', 'channel', 'baseline', 'local', 'online', 'state')
        self.sources = ttk.Treeview(source_frame, columns=cols, show='headings', height=9, selectmode='browse')
        source_cols = [('GitHub 来源', 290), ('通道', 92), ('首次完整包', 132), ('本地最近包', 132), ('在线版本', 132), ('比较结果', 127)]
        self.columns(self.sources, cols, source_cols)
        self.sources.tag_configure('alt', background='#f6f9fc')
        self.scroll_tree(source_frame, self.sources)
        self.sources.bind('<<TreeviewSelect>>', lambda _: self.render_details())

        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill='both', expand=True, pady=(11, 5))
        versions = ttk.Frame(self.tabs, padding=9)
        packages = ttk.Frame(self.tabs, padding=9)
        activity = ttk.Frame(self.tabs, padding=9)
        about = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(versions, text='组件版本')
        self.tabs.add(packages, text='USB 部署包')
        self.tabs.add(activity, text='构建输出')
        self.tabs.add(about, text='关于')

        line = ttk.Frame(versions)
        line.pack(fill='x', pady=(0, 6))
        self.source_hint = ttk.Label(line, foreground=MUTED)
        self.source_hint.pack(side='left', fill='x', expand=True)
        self.button(line, '记录车机版本', self.record_installed).pack(side='right')
        comp_cols = ('id', 'baseline', 'local', 'online', 'car')
        self.components = ttk.Treeview(versions, columns=comp_cols, show='headings', height=5, selectmode='browse')
        self.columns(self.components, comp_cols, [('组件 ID', 270), ('首次完整包', 115), ('本地最近包', 115),
                                                  ('在线版本', 115), ('车机已确认', 140)])
        self.components.tag_configure('alt', background='#f6f9fc')
        self.scroll_tree(versions, self.components)
        self.components.bind('<Double-1>', lambda _: self.record_installed())

        package_actions = ttk.Frame(packages)
        package_actions.pack(fill='x', pady=(0, 6))
        self.button(package_actions, '读取选中包的版本', self.load_package).pack(side='left')
        self.button(package_actions, '记录已部署', self.record_package).pack(side='left', padx=(7, 0))
        self.button(package_actions, '刷新列表', self.refresh_packages).pack(side='left', padx=7)
        self.package_count = ttk.Label(package_actions, foreground=MUTED)
        self.package_count.pack(side='right')
        package_cols = ('name', 'date', 'sources', 'mode')
        self.packages = ttk.Treeview(packages, columns=package_cols, show='headings', height=5, selectmode='browse')
        self.columns(self.packages, package_cols, [('USB 中的部署 ZIP', 360), ('打包时间', 150), ('来源数', 90), ('类型', 115)])
        self.scroll_tree(packages, self.packages)
        self.packages.bind('<Double-1>', lambda _: self.load_package())

        self.activity = self.scroll_text(activity, wrap='word')
        self.create_about(about)

        footer = ttk.Frame(body)
        footer.pack(fill='x', pady=(3, 0))
        self.progress = ttk.Progressbar(footer, length=174, maximum=9)
        self.progress.pack(side='right')
        ttk.Label(footer, textvariable=self.status, foreground=MUTED).pack(side='left', fill='x', expand=True)

    @staticmethod
    def columns(tree, keys, spec):
        for key, (label, width) in zip(keys, spec):
            tree.heading(key, text=label, anchor='w')
            tree.column(key, width=width, minwidth=min(width, 76), stretch=True)

    def create_about(self, parent):
        ttk.Label(parent, text=f'BMW MGU 离线仓库管理器 {VERSION}',
                  font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', pady=(0, 6))
        notice = self.scroll_text(parent, wrap='word')
        self.put_text(notice,
                      '版权与第三方许可\n'
                      '© 2026 Kan Ning 及本项目贡献者。第三方软件、插件和发布资源的著作权归各自权利人，适用各项目的许可。\n'
                      '随附的 MGU Store mgu-install.js 由 Robert Nisipeanu 及其贡献者维护，采用 GPL-3.0-or-later。'
                      'Node.js、Python、Tcl/Tk 和 PyInstaller 的许可随程序附在 source/licenses。\n\n'
                      '独立工具声明\n'
                      'BMW、iDrive 和相关标识归各自权利人。本工具是独立的电脑端仓库管理器，与 BMW 官方和上游项目无隶属、授权或背书关系。\n'
                      '部署由你在车机 MGU Store 中执行，部署记录以你在车机核对的结果为准。'
                      '软件按现状提供，第三方插件的车机兼容性、运行稳定性和回退能力取决于车机环境及上游项目。')

    @staticmethod
    def scroll_tree(parent, tree):
        scroll = ttk.Scrollbar(parent, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)

    @staticmethod
    def scroll_text(parent, wrap):
        widget = tk.Text(parent, height=5, width=36, padx=11, pady=8, relief='flat', wrap=wrap,
                         bg=WHITE, fg=INK, state='disabled', highlightthickness=1, highlightbackground=LINE)
        scroll = ttk.Scrollbar(parent, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        widget.pack(side='left', fill='both', expand=True)
        return widget

    @staticmethod
    def put_text(widget, content, append=False):
        widget.configure(state='normal')
        if not append:
            widget.delete('1.0', 'end')
        widget.insert('end', content)
        if append:
            widget.see('end')
        widget.configure(state='disabled')

    @staticmethod
    def clear(tree):
        if tree.get_children():
            tree.delete(*tree.get_children())

    def save(self):
        builder.write_json(self.state_path, self.state)

    def selected(self):
        selection = self.sources.selection()
        return selection[0] if selection else next(iter(self.active_sources), None)

    def snapshot(self, key):
        return lib.by_folder(self.state.get(key))

    def render_sources(self, label=None):
        label = label or self.selected()
        self.clear(self.sources)
        base, local, online = [self.snapshot(key) for key in ('baseline', 'local', 'online')]
        for i, (folder, source) in enumerate(self.active_sources.items()):
            up = online.get(folder)
            old = local.get(folder)
            compare = '待检查'
            if up and up.get('state') == 'stale':
                compare = '在线查询失败'
            elif up and old:
                compare = '与在线一致' if lib.revision(old) and lib.revision(up) == lib.revision(old) else '与在线不同'
            elif up:
                compare = '待生成本地包'
            channel = lib.CHANNELS[source['channel']]
            if source['channel'] == 'branch':
                channel = source.get('branch', 'main')
                channel = '默认分支' if channel == 'HEAD' else channel + ' 分支'
            self.sources.insert('', 'end', iid=folder, tags=('alt',) if i % 2 else (),
                                values=(source['repo'], channel, lib.repo_version(base.get(folder)),
                                        lib.repo_version(old), lib.repo_version(up), compare))
        label = label if label in self.active_sources else next(iter(self.active_sources), None)
        if label:
            self.sources.selection_set(label)
            self.sources.focus(label)
            self.sources.see(label)
        self.source_count.configure(text=f'{len(self.active_sources)} 个来源')
        timestamps = [local_time((self.state.get(key) or {}).get('generated_utc')) for key in ('baseline', 'local', 'online')]
        cached = any('CACHED' in row.get('freshness', '') for row in local.values())
        local_label = timestamps[1] + ('（缓存）' if cached else '')
        deployed = local_time((self.state.get('deployed') or {}).get('recorded_utc'))
        self.summary.configure(text=f'首次基线  {timestamps[0]}   |   本地最近包  {local_label}   |   已部署记录  {deployed}   |   上次联网检查  {timestamps[2]}')
        self.render_details()

    def render_details(self):
        label = self.selected()
        rows = [self.snapshot(key).get(label) or {} for key in ('baseline', 'local', 'online')]
        records = self.state.get('installed', {}).get(label, {})
        recorded = self.snapshot('deployed').get(label, {}).get('inventory', {})
        versions = [dict(row.get('items', [])) for row in rows]
        self.clear(self.components)
        ids = sorted(set().union(*(x.keys() for x in versions), records.keys(), recorded.keys()))
        for index, ident in enumerate(ids):
            self.components.insert('', 'end', iid=ident, tags=('alt',) if index % 2 else (),
                                   values=(ident, *(x.get(ident) or '—' for x in versions),
                                           records.get(ident, recorded.get(ident, {}).get('version', '—'))))
        up = rows[-1]
        details = f'{len(ids)} 个组件  ·  车机栏按 Installed 页手动确认' if label else '添加 GitHub 来源后即可检查版本与打包。'
        if up.get('items_error'):
            details = '已取得在线提交，组件清单读取失败。可在「构建输出」查看错误。'
        if up.get('state') == 'stale':
            details = '在线查询失败。在线列显示上次成功记录（如有）。'
        self.source_hint.configure(text=details)

    def add_source(self):
        panel = tk.Toplevel(self.root)
        panel.title('添加 GitHub 来源')
        panel.transient(self.root)
        panel.grab_set()
        form = ttk.Frame(panel, padding=17)
        form.pack(fill='both', expand=True)
        repo, branch = tk.StringVar(), tk.StringVar()
        ttk.Label(form, text='GitHub 链接或 owner/repo').grid(row=0, column=0, sticky='w', pady=5)
        entry = ttk.Entry(form, width=47, textvariable=repo)
        entry.grid(row=0, column=1, sticky='ew', padx=(12, 0))
        ttk.Label(form, text='更新通道').grid(row=1, column=0, sticky='w', pady=5)
        channels = ('自动识别', *lib.CHANNELS.values())
        channel = ttk.Combobox(form, values=channels, state='readonly', width=23)
        channel.current(0)
        channel.grid(row=1, column=1, sticky='w', padx=(12, 0))
        ttk.Label(form, text='分支名（留空用默认分支）').grid(row=2, column=0, sticky='w', pady=5)
        branch_entry = ttk.Entry(form, width=27, textvariable=branch, state='disabled')
        branch_entry.grid(row=2, column=1, sticky='w', padx=(12, 0))
        channel.bind('<<ComboboxSelected>>', lambda _: branch_entry.configure(state='normal' if channel.get() == '分支' else 'disabled'))

        def submit():
            try:
                normalized = lib.normalize_repo(repo.get())
                if len(self.active_sources) >= 64:
                    raise builder.BuildFailure('MGU Store 的 USB 仓库最多读取 64 个来源。')
                if any(s['repo'].casefold() == normalized.casefold() for s in self.active_sources.values()):
                    raise builder.BuildFailure('该 GitHub 仓库已在来源列表中。')
                key = next((k for k, v in lib.CHANNELS.items() if v == channel.get()), None)
                source = lib.make_source(normalized, key, branch.get()) if key else None
            except (ValueError, builder.BuildFailure) as ex:
                messagebox.showerror('添加 GitHub 来源', str(ex), parent=panel)
                return
            panel.destroy()
            if source:
                self.save_source(source)
            else:
                _, proxy, _, _ = self.options()
                self.run('正在识别 GitHub 来源…', lambda: lib.detect_source(normalized, proxy), self.save_source)

        ttk.Button(form, text='添加', command=submit, style='Primary.TButton').grid(row=3, column=1, sticky='e', pady=(12, 0))
        panel.bind('<Return>', lambda _: submit())
        panel.bind('<Escape>', lambda _: panel.destroy())
        entry.focus_set()

    def save_source(self, source):
        label = source['folder']
        self.active_sources[label] = source
        self.save_sources()
        self.render_sources(label)
        self.status.set(f'已添加 {source["repo"]}')

    def remove_source(self):
        label = self.selected()
        if not label:
            return
        repo = self.active_sources[label]['repo']
        if not messagebox.askyesno('删除来源', f'从今后的版本检查与部署 ZIP 中移除 {repo}？', parent=self.root):
            return
        del self.active_sources[label]
        self.save_sources()
        self.render_sources()
        self.status.set(f'已从来源列表删除 {repo}')

    def save_sources(self):
        self.state['sources'] = list(self.active_sources.values())
        self.save()

    def open_output(self):
        try:
            self.usb_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.usb_dir))
        except OSError as ex:
            self.show_error(str(ex))

    def refresh_packages(self):
        self.clear(self.packages)
        self.package_paths = {}
        for index, (path, date, count, kind) in enumerate(lib.list_usb_packages(self.usb_dir)):
            key = str(index)
            self.package_paths[key] = path
            self.packages.insert('', 'end', iid=key, values=(path.name, local_time(date), count, kind))
        self.package_count.configure(text=f'{len(self.package_paths)} 个部署包')
        if self.package_paths:
            self.packages.selection_set('0')

    def load_package(self):
        if self.packages.selection() and not self.busy:
            self.read_package(self.package_paths[self.packages.selection()[0]])

    def record_package(self):
        if not self.packages.selection():
            self.status.set('请先选中已在车机部署的包。')
            return
        path = self.package_paths[self.packages.selection()[0]]
        self.run('正在校验选中的部署包…', lambda: lib.import_library(path), self.confirm_deployment)

    def confirm_deployment(self, report):
        if report.get('mode') == 'delta' and not self.state.get('deployed'):
            raise builder.BuildFailure('请先记录一个全量部署包。')
        panel = tk.Toplevel(self.root)
        panel.title('记录车机已部署的组件')
        panel.transient(self.root)
        panel.grab_set()
        form = ttk.Frame(panel, padding=15)
        form.pack(fill='both', expand=True)
        ttk.Label(form, text='按车机 MGU Store → Installed 的实际结果选择，支持 Ctrl / Shift 多选。').pack(anchor='w', pady=(0, 8))
        middle = ttk.Frame(form)
        middle.pack(fill='both', expand=True)
        rows = {}
        tree = ttk.Treeview(middle, columns=('repo', 'id', 'version'), show='headings', height=12, selectmode='extended')
        self.columns(tree, ('repo', 'id', 'version'), [('GitHub 来源', 265), ('组件 ID', 210), ('版本', 95)])
        self.scroll_tree(middle, tree)
        for row in report['sources']:
            for ident, version in row['items']:
                key = str(len(rows))
                rows[key] = (row['folder'], ident)
                tree.insert('', 'end', iid=key, values=(row['repo'], ident, version))

        def save_record():
            selected = {rows[key] for key in tree.selection()}
            if not selected:
                messagebox.showinfo('记录已部署', '请至少选择一个已部署组件。', parent=panel)
                return
            self.state['deployed'] = delta.deployed_after(self.state.get('deployed'), report, selected)
            # The confirmed package version supersedes any older manually entered display value.
            for label, ident in selected:
                self.state.get('installed', {}).get(label, {}).pop(ident, None)
            self.save()
            self.render_sources()
            self.status.set(f'已记录 {len(selected)} 个车机组件')
            panel.destroy()

        actions = ttk.Frame(form)
        actions.pack(fill='x', pady=(8, 0))
        ttk.Button(actions, text='全选', command=lambda: tree.selection_set(tree.get_children())).pack(side='left')
        ttk.Button(actions, text='清空选择', command=lambda: tree.selection_remove(tree.selection())).pack(side='left', padx=6)
        ttk.Button(actions, text='保存部署记录', command=save_record, style='Primary.TButton').pack(side='right')
        panel.bind('<Escape>', lambda _: panel.destroy())
        panel.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - panel.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - panel.winfo_reqheight()) // 2
        panel.geometry(f'+{max(0, x)}+{max(0, y)}')

    def options(self):
        return (Path(self.state.get('cache') or self.data/'cache'), self.state.get('proxy') or None,
                self.state.get('hashes', True), self.state.get('fail_fast', False))

    def settings(self):
        panel = tk.Toplevel(self.root)
        panel.title('设置')
        panel.configure(bg=BG)
        panel.transient(self.root)
        panel.grab_set()
        form = ttk.Frame(panel, padding=17)
        form.pack(fill='both', expand=True)
        cache, proxy, hashes, fail_fast = self.options()
        values = [tk.StringVar(value=str(cache)), tk.StringVar(value=proxy or '')]
        for i, (name, value) in enumerate(zip(('下载缓存目录', 'HTTP 代理（可留空）'), values)):
            ttk.Label(form, text=name).grid(row=i, column=0, sticky='w', pady=6)
            ttk.Entry(form, width=57, textvariable=value).grid(row=i, column=1, sticky='ew', padx=8)
            if i == 0:
                def choose(v=value):
                    path = filedialog.askdirectory(parent=panel)
                    if path:
                        v.set(path)
                ttk.Button(form, text='浏览…', command=choose).grid(row=i, column=2)
        hash_var, fail_var = tk.BooleanVar(value=hashes), tk.BooleanVar(value=fail_fast)
        ttk.Checkbutton(form, text='附加全库 SHA-256 文件清单', variable=hash_var).grid(row=3, column=1, sticky='w', pady=3)
        ttk.Checkbutton(form, text='遇到第一个失败来源时停止', variable=fail_var).grid(row=4, column=1, sticky='w', pady=3)
        ttk.Label(form, text='可选 GitHub API 令牌从 GITHUB_TOKEN 环境变量读取。', foreground=MUTED).grid(row=5, column=1, sticky='w', pady=8)

        def commit():
            cache_text, proxy_text = (x.get().strip() for x in values)
            if not cache_text or (proxy_text and not proxy_text.startswith(('http://', 'https://'))):
                messagebox.showerror('设置', '请填写目录，并使用 http:// 或 https:// 代理地址。', parent=panel)
                return
            self.state.update(cache=str(Path(cache_text).expanduser().resolve()),
                              proxy=proxy_text, hashes=hash_var.get(), fail_fast=fail_var.get())
            self.save()
            panel.destroy()

        ttk.Button(form, text='保存', command=commit, style='Primary.TButton').grid(row=6, column=2, pady=(10, 0))
        panel.wait_window()

    def record_installed(self):
        if self.busy or not self.components.selection():
            return
        label, ident = self.selected(), self.components.selection()[0]
        old, local, online, installed = self.components.item(ident, 'values')[1:]
        initial = next((v for v in (installed, local, old, online) if v != '—'), '')
        value = simpledialog.askstring('记录车机版本', f'{ident}\n输入车机 Store → Installed 显示的版本。留空可清除记录。',
                                       initialvalue=initial, parent=self.root)
        if value is None:
            return
        records = self.state.setdefault('installed', {}).setdefault(label, {})
        if value.strip():
            records[ident] = value.strip()
        else:
            records.pop(ident, None)
            recorded = self.snapshot('deployed').get(label, {}).get('inventory', {})
            recorded.pop(ident, None)
        self.save()
        self.render_details()
        if self.components.exists(ident):
            self.components.selection_set(ident)
            self.components.see(ident)

    def run(self, name, work, finished, log=False):
        if self.busy:
            return
        self.busy = True
        for button in self.controls:
            button.state(['disabled'])
        self.progress.configure(value=0)
        self.status.set(name)
        if log:
            self.tabs.select(2)
            self.put_text(self.activity, '')

        def worker():
            try:
                result = work()
                self.events.put(('done', (finished, result)))
            except Exception as ex:
                self.events.put(('error', str(ex)))

        threading.Thread(target=worker, daemon=True).start()

    def emit(self, value):
        if value:
            self.events.put(('log', value))

    def advance(self, step, total, label):
        self.events.put(('progress', (step, total, label)))

    def poll(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == 'log':
                    self.put_text(self.activity, payload + '\n', True)
                elif event == 'progress':
                    step, total, label = payload
                    self.progress.configure(maximum=total, value=step)
                    self.status.set(f'{step} / {total}   {label}')
                else:
                    self.busy = False
                    for button in self.controls:
                        button.state(['!disabled'])
                    self.progress.configure(value=0)
                    if event == 'done':
                        fn, result = payload
                        try:
                            fn(result)
                        except Exception as ex:
                            self.show_error(str(ex))
                    else:
                        self.show_error(payload)
        except queue.Empty:
            pass
        self.root.after(70, self.poll)

    def show_error(self, error):
        self.status.set('操作失败 · 详情见构建输出')
        self.put_text(self.activity, error + '\n', True)
        messagebox.showerror('操作失败', error, parent=self.root)

    def check(self):
        sources = self.configured_sources()
        if not sources:
            return
        cache, proxy, _, _ = self.options()
        previous = self.state.get('online')
        snapshots = {label: [snap[label] for key in ('online', 'local', 'baseline') if label in (snap := self.snapshot(key))]
                     for label in self.active_sources}

        def done(result):
            self.state['online'] = result
            self.save()
            self.render_sources()
            fresh = sum(row.get('state') == 'checked' for row in result['sources'])
            self.status.set(f'在线版本已确认 {fresh} / {len(sources)} 个来源')
            for row in result['sources']:
                if row.get('error') or row.get('items_error'):
                    self.emit(f'{row["repo"]} · {row.get("error") or row["items_error"]}')

        self.run('正在检查 GitHub…', lambda: lib.check_online(cache, proxy, previous, snapshots, self.advance, sources), done)

    def configured_sources(self):
        sources = list(self.active_sources.values())
        if not sources:
            self.status.set('请先添加至少一个 GitHub 来源。')
        return sources

    def set_local(self, snapshot):
        self.state.setdefault('baseline', snapshot)
        self.state['local'] = snapshot
        self.save()
        self.render_sources()

    def import_snapshot(self, folder):
        name = (filedialog.askdirectory(parent=self.root, title='选择包含 mgu-repo 的目录') if folder else
                filedialog.askopenfilename(parent=self.root, title='选择已生成的完整部署包',
                                           initialdir=self.usb_dir if self.usb_dir.is_dir() else self.usb_dir.parent,
                                           filetypes=[('部署 ZIP', '*.zip')]))
        if name:
            self.read_package(Path(name))

    def read_package(self, path):
        def done(snapshot):
            if snapshot.get('mode') == 'delta':
                self.show_delta(path, snapshot)
                return
            self.set_local(snapshot)
            self.tabs.select(0)
            total = len(snapshot['sources'])
            shown = len(lib.by_folder(snapshot).keys() & self.active_sources.keys())
            self.status.set(f'已读取 {total} 个来源的本地版本' + (f' · 当前列表显示其中 {shown} 个' if shown != total else ''))

        self.run('正在读取版本并校验部署文件…', lambda: lib.import_library(path), done)

    def package_path(self, kind):
        date = dt.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        path = self.usb_dir/f'BMW_MGU_{kind}_{date}.zip'
        return path.with_stem(path.stem + '_' + uuid.uuid4().hex[:8]) if path.exists() else path

    def show_delta(self, path, report):
        self.tabs.select(2)
        lines = [path.name, '']
        for row in report['sources']:
            lines.append(row['repo'])
            for ident, version in row['items']:
                reason = '更新' if ident in row.get('updated_ids', []) else '依赖'
                lines.append(f'  {ident}  {version}  [{reason}]')
            lines.append('')
        self.put_text(self.activity, '\n'.join(lines))
        self.status.set(f'增量包 · {report["updated_count"]} 个更新')

    def export_updates(self):
        sources = self.configured_sources()
        if not sources:
            return
        deployed = self.state.get('deployed')
        if not deployed:
            self.tabs.select(1)
            self.status.set('请先选中全量包，点击「记录已部署」。')
            return
        deployed = delta.apply_manual_versions(deployed, self.state.get('installed', {}), sources)
        cache, proxy, hashes, _ = self.options()
        path = self.package_path('Delta')

        def done(report):
            if report is None:
                self.status.set('当前来源没有待部署的更新。')
                return
            self.refresh_packages()
            self.tabs.select(1)
            count = report['updated_count']
            self.status.set(f'增量 ZIP 已生成 · {count} 个更新')
            messagebox.showinfo('增量 ZIP 已生成', f'{path}\n\n请解压到干净的 U 盘目录。车机更新完成后，在「USB 部署包」记录实际部署的组件。', parent=self.root)

        self.run('正在核对已部署版本与上游…', lambda: delta.export_delta(path, cache, proxy, hashes, sources,
                                                                          deployed, self.emit, self.advance), done, log=True)

    def export(self, offline=False, as_zip=True):
        sources = self.configured_sources()
        if not sources:
            return
        cache, proxy, hashes, fail_fast = self.options()
        if as_zip:
            path = self.package_path('Cache' if offline else 'Full')
        else:
            name = filedialog.askdirectory(parent=self.root, title='选择 U 盘根目录或写入文件夹')
            if not name:
                return
            path = Path(name).resolve()

        def done(result):
            rc, snapshot, written = result
            complete = lib.complete_report(snapshot, sources)
            if written and complete:
                if not rc:
                    self.state['online'] = {'generated_utc': snapshot['generated_utc'],
                                            'sources': [dict(row, state='checked', checked_utc=snapshot['generated_utc']) for row in snapshot['sources']]}
                self.set_local(snapshot)
                if as_zip:
                    self.refresh_packages()
                    self.tabs.select(1)
                caption = '完整 ZIP 已生成' if as_zip else '完整仓库已写入'
                if rc:
                    caption += ' · 含未确认最新的缓存'
                self.status.set(caption)
                messagebox.showinfo(caption, f'{path}\n\n在 U 盘根目录保留 mgu-repo 文件夹。\n在车机 MGU Store → USB 逐项安装或更新。', parent=self.root)
            else:
                self.status.set('有来源未完成 · 请查看构建输出')
                tip = '目标 ZIP 保留原状。' if as_zip else '已写入成功来源。失败来源沿用目标中已有文件。'
                messagebox.showwarning('完整部署包未就绪', tip + '\n\n详见「构建输出」。', parent=self.root)

        self.run('正在生成车机仓库…', lambda: lib.export_library(path, cache, proxy, offline, hashes, fail_fast,
                                                               self.emit, self.advance, sources, as_zip), done, log=True)

    def close(self):
        if self.busy:
            self.status.set('当前操作仍在进行，完成后可关闭窗口。')
        else:
            self.root.destroy()


def main():
    if sys.platform == 'win32':
        import ctypes
        import msvcrt

        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2))
    root = tk.Tk()
    data = Path(os.getenv('BMW_MGU_MANAGER_DATA') or (Path(os.getenv('LOCALAPPDATA') or Path.home())/'BMW MGU Offline Manager'))
    data.mkdir(parents=True, exist_ok=True)
    with (data/'manager.lock').open('a+b') as lock:
        if sys.platform == 'win32':
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                root.withdraw()
                messagebox.showinfo('BMW MGU 离线仓库', '管理器已打开。请使用现有窗口。', parent=root)
                root.destroy()
                return
        try:
            program = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
            Manager(root, data, program/'USB')
            root.mainloop()
        except Exception as ex:
            root.withdraw()
            messagebox.showerror('管理器启动失败', str(ex), parent=root)
            root.destroy()


if __name__ == '__main__':
    main()
