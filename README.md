# md2pdf

Converts Markdown to a paginated PDF laid out for a **reMarkable Paper Pro** (the default), another reMarkable, or paper.
The PDF has a cover, a contents page with page numbers, PDF bookmarks, clickable internal links, and page numbers.

It needs only Windows and Microsoft Edge. Python and everything else install themselves on first run, with no admin rights and no global install.

## Get it

Clone the repository, or download it as a ZIP (**Code → Download ZIP**) and extract it anywhere:

```powershell
git clone https://github.com/jorgll/md2pdf.git
cd md2pdf
.\md2pdf.cmd --setup              # optional: install and check everything now
```

If you downloaded the ZIP, unblock the scripts once so Windows doesn't flag them:

```powershell
Get-ChildItem . | Unblock-File
```

## Quick start

```powershell
.\md2pdf.cmd notes.md              # writes notes.pdf next to notes.md
.\md2pdf.cmd notes.md -o C:\pdfs   # PDF into another folder
.\md2pdf.ps1 notes.md              # the same, from PowerShell
```

You can also drag one or more `.md` files, or a folder, onto `md2pdf.cmd` in Explorer. PDFs land next to the Markdown files.

To run `md2pdf` from any folder, add this folder to your user PATH once:

```powershell
.\md2pdf.cmd --add-to-path         # then, in a new terminal: md2pdf notes.md
```

## Options

```
md2pdf [options] INPUT...

INPUT                  Markdown files, folders (their .md files), or wildcards such as docs\*.md
-o, --output PATH      output PDF (one input) or folder (default: next to each input)
-d, --device DEVICE    page size to lay out for (default: paper-pro)
--layout auto|book|compact
                       book: cover page and a contents page with page numbers
                       compact: title at the top of page 1, no cover
                       auto (default): book for documents of 6+ pages with 4+ sections
--breaks auto|on|off   start each top-level section on a new page
                       (auto: in book layout, unless it wastes many pages)
--font-size PT         body text size in points (default depends on the device)
--no-section-links     don't turn "§4.2"-style references into links to heading 4.2
--no-mermaid           show mermaid diagrams as source code (and never download mermaid.js)
--html                 also save the rendered HTML next to each PDF
--json                 print a JSON report instead of text (for scripts and agents)
--browser MSEDGE       path to msedge.exe (default: found automatically)
--setup                install and check everything (Python packages, Edge, mermaid.js), then exit
-q, --quiet            print only warnings and errors
--version, --help
--add-to-path          (launchers only) add this folder to your user PATH
```

### Devices

| `-d` | Page size (mm) | Body text |
|---|---|---|
| `paper-pro` (default) | 180 × 240, the Paper Pro screen | 11 pt |
| `rm2` | 157.5 × 210, the reMarkable 2 screen | 10.5 pt |
| `move` | 91.8 × 163.2, the Paper Pro Move screen | 9.5 pt |
| `a5`, `a4`, `letter` | paper sizes | 10–11 pt |
| `150x200mm`, `15x20cm`, `6x8in` | any custom size | by width |

The reMarkable pages match the physical screen, so pages fill the screen at 1:1 with no zooming.

### About "§" links

By default, `§4.2`-style references are linked to heading 4.2 of the same document, and the command prints how many it linked. If a document uses "§" to point into *other* documents, use `--no-section-links`.

## What it renders

- CommonMark plus tables, strikethrough, task lists, footnotes, and YAML front matter (`title`, `author`, `date`, `description`).
- GitHub alerts: `> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`, `[!CAUTION]`.
- Azure DevOps wiki syntax: `[[_TOC_]]`, `:::mermaid` blocks, `#Heading` without a space, `![alt](img.png =300x)` image sizes, and `/.attachments/...` images (found by walking up the folders).
- Mermaid diagrams, in ```` ```mermaid ```` fences or `:::mermaid` blocks, drawn as vector graphics.
- Inline HTML such as `<details>` (printed expanded), `<kbd>`, `<sub>` and `<img>`. Scripts, styles, forms, frames and event handlers are removed.
- Code blocks and wide tables are shrunk or wrapped to fit the page. Nothing is cut off at the right edge, and the command warns if something still doesn't fit.

## What the first run installs, and where

Everything goes in `.runtime\` next to these scripts. If that folder isn't writable, it goes in `%LOCALAPPDATA%\md2pdf`. Set `MD2PDF_HOME` to choose another folder.

| Folder | What | When |
|---|---|---|
| `lib-py3XX-*` | Python packages `markdown-it-py`, `mdit-py-plugins`, `pypdf`, `websockets` (pip `--target`, so your Python is not touched) | first run |
| `python\` | the official embeddable Python from python.org (not added to PATH) | only if no Python 3.10+ is found |
| `cache\mermaid.min.js` | mermaid.js from jsDelivr (unpkg as fallback) | first document with a mermaid diagram, or `--setup` |
| `tmp\` | scratch files for a run, deleted afterwards | every run |

**Uninstall:** delete this folder (and `%LOCALAPPDATA%\md2pdf` if it was used). If you ran `--add-to-path`, also remove the folder from your user PATH.

Python is chosen in this order: `MD2PDF_PYTHON` (a path to python.exe, or `private` to force the private copy), then `.runtime\python`, then `py -3`, then `python` on PATH. The Microsoft Store placeholder `python.exe` is skipped.

## Privacy

md2pdf was built to be safe for sensitive documents:

- **Documents never leave this PC.** Pages are rendered by Microsoft Edge running headless on this machine, with a new throwaway profile for each run, deleted at the end. Edge starts with background networking, component updates, crash reporting and pings turned off.
- **Only Microsoft Edge is supported, by design.** md2pdf never downloads or runs another browser.
- **Network use:**
  - PyPI (your configured pip index) on the first run.
  - python.org, only if it has to install Python.
  - jsDelivr or unpkg, once, for mermaid.js. The download contains nothing from your documents. To avoid it, use `--no-mermaid`, or set `MD2PDF_MERMAID_URL` to your own copy (a URL or a local file path).
- **Remote images:** if a document embeds an image by `https://` URL, Edge fetches that image the way a browser would, so its server sees the request. Local and relative images are read from disk.

## For scripts and agents

- `--json` prints one record per input, with:
  - `ok`, `output`, `pages`, `layout`, `chapter_breaks`;
  - `warnings` (for example content wider than the page, missing images, or a mermaid diagram that failed);
  - bookmark and link counts.
- Exit codes: `0` all done; `1` at least one file failed; `2` bad command line; `3` setup problem (Python, packages, Edge, or a policy that blocks Edge's DevTools).
- `MD2PDF_DEBUG=1` prints Python tracebacks for failures.

## Troubleshooting

- **"running scripts is disabled on this system"**: use `md2pdf.cmd`, which runs PowerShell with `-ExecutionPolicy Bypass`. If you copied the folder from a download or email, you can also run `Get-ChildItem <folder> | Unblock-File`.
- **"md2pdf cannot drive Edge on this PC"** or **"could not start headless Edge"**: md2pdf prints through Edge's DevTools protocol. The Edge group policies `RemoteDebuggingAllowed`, `DeveloperToolsAvailability` and `HeadlessModeEnabled` can turn that off. The error names the policy that was found. Only your IT admin can change it.
- **Proxy or offline machine**: pip uses your normal pip configuration (`pip.ini`, `PIP_INDEX_URL`). You can pre-seed mermaid with `MD2PDF_MERMAID_URL=C:\path\mermaid.min.js`.
- **Python or package errors**: run `md2pdf --setup` to see which Python and packages md2pdf found. Set `MD2PDF_DEBUG=1` for full tracebacks. To ignore any installed Python and use a private copy, set `MD2PDF_PYTHON=private`.

### Start from scratch

To reset md2pdf to a clean first run:

1. Delete the `.runtime\` folder next to the scripts.
2. Delete `%LOCALAPPDATA%\md2pdf` if it exists (used when `.runtime\` isn't writable), and the `MD2PDF_HOME` folder if you set one.
3. Run `.\md2pdf.cmd --setup`. It reinstalls the packages, checks Edge, downloads mermaid.js, and ends with `ready`.

```powershell
Remove-Item .runtime, "$env:LOCALAPPDATA\md2pdf" -Recurse -Force -ErrorAction SilentlyContinue
.\md2pdf.cmd --setup
```

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
