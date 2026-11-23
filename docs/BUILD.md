# Building the Windows release

Use Windows x64 with CPython 3.14.6 and Tkinter. Install PyInstaller 6.22.3 in the selected Python environment, then run the supplied PowerShell build script from the repository root.

```powershell
python -m pip install pyinstaller==6.22.3 pyinstaller-hooks-contrib==2026.10
powershell -File tools/build_windows.ps1
```

The script fetches Node.js 26.13.1 from the official distribution, verifies its SHA-256, verifies the vendored installer and packages `app/manager.py`. Runtime files are placed inside `_internal`. Python source and license texts are copied into the portable directory. The repository's tagged GitHub source archive contains the vendored installer and build script as well.

For local development with Python, place a verified `node.exe` and a copy of `vendor/mgu-install.js` beside `app/manager.py`, then run `python app/manager.py`. The GUI's portable builder reads those files relative to `library.py`. The command-line entry point remains `python app/build_mgu_usb.py --help`.

The bundled upstream installer is pinned to commit `407f13e2091a028f7855568c51ba36f10dfab40d`. The GUI uses its `usb-repo` command. The standalone command-line builder retains the existing online installer refresh and offline cache behavior. Runtime and source links are listed in [third-party notices](../THIRD_PARTY_NOTICES.md).

Application settings, inventories and caches belong to the user's local data directory. They are excluded from source control and release packaging. A development or release screenshot should use an isolated data directory, such as one selected with `BMW_MGU_MANAGER_DATA`, and public source metadata.

For 1.2.1, the four application modules were compared with the delivered 1.2.0 source. Only `VERSION` and the About attribution text in `manager.py` differ after normalizing line endings. Windows executable startup and both screens were inspected after packaging.
