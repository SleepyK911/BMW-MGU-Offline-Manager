# Third-party notices

Copyright © 2026 Kan Ning and contributors for the original manager code and documentation. The project is distributed under GPL-3.0-or-later. Upstream software and downloaded plugins retain their respective copyrights and licenses.

| Component | Origin and version | License text |
| --- | --- | --- |
| MGU Store installer | `robertnisipeanu/mgu-store`, commit `407f13e2091a028f7855568c51ba36f10dfab40d`, unmodified `deploy/mgu-install.js` | [GPL-3.0](licenses/MGU-Store-GPL-3.0.txt), GPL-3.0-or-later as stated upstream |
| Node.js runtime | Windows x64 26.13.1, official release binary | [Node.js and bundled dependency licenses](licenses/Node.js-LICENSE.txt) |
| Python runtime | CPython 3.14.6, included by PyInstaller in the Windows release | [Python license and acknowledgments](licenses/Python-LICENSE.txt) |
| Tcl/Tk | Included by the Python distribution and PyInstaller | [Tcl/Tk notices](licenses/Tcl-Tk-license.terms), also retained in the packaged runtime where supplied |
| PyInstaller | 6.22.3 bootloader and packaging tooling | [License with bootloader exception](licenses/PyInstaller-COPYING.txt) |
| PyInstaller hooks | 2026.10 build-time hooks | [Hook licenses](licenses/PyInstaller-hooks-LICENSE.txt) |

The upstream installer source is included in [vendor/mgu-install.js](vendor/mgu-install.js). Its SHA-256 is `338c9f1d6857162a64361ed69541d234eef0e189c3d29d0c57e2c32ef89cd456`.

The Node.js executable is included in the Windows release. Its SHA-256 is `f5109dc868ec38703ad8f305e0aceece717cb60d4889873f434017af4e96cccb`. The build script fetches that version from the official distribution and verifies the hash.

Corresponding runtime sources are available from [Node.js 26.13.1](https://nodejs.org/dist/v26.13.1/), [CPython 3.14.6](https://www.python.org/ftp/python/3.14.6/), [Tcl/Tk](https://www.tcl-lang.org/software/tcltk/download.html), and [PyInstaller 6.22.3](https://github.com/pyinstaller/pyinstaller/tree/v6.22.3). Runtime notices supplied in the portable package apply to their respective bundled libraries.

The manager fetches third-party application artifacts and repository snapshots at the user's request. These downloads remain governed by each upstream project's license. Source catalogs and `mgu-copy.json` preserve upstream identities. BMW OAP SDK files, user caches, deployment inventories and credentials are excluded from this repository and the portable application release.
