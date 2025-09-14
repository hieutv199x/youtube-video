import sys
import shutil
import subprocess
from pathlib import Path
import argparse
import textwrap
import urllib.request
import zipfile
import io
import os

PROJECT_ROOT = Path(__file__).parent
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
MAIN_ENTRY = "app/main.py"
APP_NAME = "YouTubeManager"
ICON_MAC = "icons/app.icns"
ICON_WIN = "icons/app.ico"
FFMPEG_BINARIES = []  # add local ffmpeg/ffprobe paths if you want to bundle them

def _pyinstaller_command():
    exe = shutil.which("pyinstaller")
    if exe:
        return [exe]
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Install with: pip install pyinstaller")
        sys.exit(1)
    return [sys.executable, "-m", "PyInstaller"]

def run(cmd):
    print(">>", " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd)

def clean():
    for p in (DIST_DIR, BUILD_DIR):
        if p.exists():
            shutil.rmtree(p)

def _auto_ffmpeg_for_platform(target: str):
    """
    If user placed ffmpeg binaries under vendor/ffmpeg/<platform>/, include them.
    Expected names: ffmpeg, ffprobe (with .exe on Windows).
    """
    plat_dir = "windows" if target == "windows" else "macos"
    base = PROJECT_ROOT / "vendor" / "ffmpeg" / plat_dir
    added = []
    if base.exists():
        for name in ("ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"):
            p = base / name
            if p.exists():
                added.append(str(p))
    # NEW: if none found and on macOS, try system (Homebrew) ffmpeg
    if not added and sys.platform == "darwin":
        brew_ffmpeg = shutil.which("ffmpeg")
        brew_ffprobe = shutil.which("ffprobe")
        if brew_ffmpeg and brew_ffprobe:
            added.append(brew_ffmpeg)
            added.append(brew_ffprobe)
    return added

def build_args(target: str, onefile: bool, debug: bool, console: bool, icon_override: str | None):
    args = [
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
    ]
    if not console:
        args.append("--windowed")
    if onefile:
        args.append("--onefile")
    if debug:
        args.append("--debug=all")
    secret = PROJECT_ROOT / "client_secret.json"
    if secret.exists():
        args += ["--add-data", f"{secret.name}:."]  # relative inside app
    for bin_path in FFMPEG_BINARIES:
        bp = Path(bin_path)
        if bp.exists():
            args += ["--add-binary", f"{bin_path}:."]
    # Auto-add ffmpeg binaries if found
    auto_bins = _auto_ffmpeg_for_platform(target)
    for b in auto_bins:
        args += ["--add-binary", f"{b}:."]
    if icon_override:
        icon_path = PROJECT_ROOT / icon_override
        if icon_path.exists():
            args += ["--icon", str(icon_path)]
    else:
        icon = ICON_WIN if target == "windows" else ICON_MAC
        if (PROJECT_ROOT / icon).exists():
            args += ["--icon", icon]
    args.append(MAIN_ENTRY)
    return args

def warn_cross_windows():
    print(textwrap.dedent("""
        Cross-building native Windows executables from macOS with PyInstaller
        is not officially supported.
        Options:
          1. Use a Windows machine / VM and run: python build.py --platform windows
          2. Use GitHub Actions workflow (added in .github/workflows/build-windows.yml)
          3. Use Wine + proper Python environment (experimental, not guaranteed)

        Recommended: push to GitHub and let the workflow produce artifacts.
    """).strip())

def parse_cli():
    ap = argparse.ArgumentParser(description="Build helper for YouTubeManager")
    ap.add_argument("--platform", choices=["macos", "windows"], default="macos")
    ap.add_argument("--onefile", action="store_true", help="Bundle into a single executable")
    ap.add_argument("--debug", action="store_true", help="Enable PyInstaller debug output")
    ap.add_argument("--console", action="store_true", help="Keep console window")
    ap.add_argument("--icon", help="Override icon path (relative)")
    ap.add_argument("--no-clean", action="store_true", help="Skip cleanup of dist/build")
    ap.add_argument("--spec-out", help="Write generated spec to this path then exit")
    ap.add_argument("--auto-ffmpeg", action="store_true",
                    help="Fetch/copy ffmpeg & ffprobe into vendor/ffmpeg/<platform>/ before build")
    return ap.parse_args()

def write_spec(spec_path: Path, pyinstaller_cmd_line: list[str]):
    spec_content = f"""# Auto-generated minimal spec
# Recreate build with: {' '.join(pyinstaller_cmd_line)}
# Edit as needed then run: pyinstaller {spec_path.name}

block_cipher = None

a = Analysis(
    ['{MAIN_ENTRY}'],
    pathex=['{PROJECT_ROOT}'],
    binaries=[],
    datas=[('client_secret.json', '.')] if (Path('{PROJECT_ROOT}')/'client_secret.json').exists() else [],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='{APP_NAME}',
    console={str(True).lower()},
)
"""
    spec_path.write_text(spec_content)
    print(f"Wrote spec: {spec_path}")

def fetch_ffmpeg_windows():
    """
    Download a recent static Windows ffmpeg build (if not already present).
    Source: BtbN GitHub builds (GPL). Adjust if you need LGPL.
    """
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
    target_dir = PROJECT_ROOT / "vendor" / "ffmpeg" / "windows"
    ffmpeg_exe = target_dir / "ffmpeg.exe"
    ffprobe_exe = target_dir / "ffprobe.exe"
    if ffmpeg_exe.exists() and ffprobe_exe.exists():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading ffmpeg (Windows) from: {url}")
    data = urllib.request.urlopen(url, timeout=60).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            lower = name.lower()
            if lower.endswith("/ffmpeg.exe") or lower.endswith("/ffprobe.exe"):
                with z.open(name) as src, open(target_dir / Path(name).name, "wb") as dst:
                    dst.write(src.read())
    print("FFmpeg (Windows) downloaded.")

def fetch_ffmpeg_macos():
    """
    macOS: if user sets AUTO_FFMPEG and no vendor copy, attempt to copy from system (brew).
    """
    target_dir = PROJECT_ROOT / "vendor" / "ffmpeg" / "macos"
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for src in (ffmpeg_bin, ffprobe_bin):
        dst = target_dir / Path(src).name
        if not dst.exists():
            shutil.copy2(src, dst)

def main():
    args = parse_cli()

    target = args.platform

    auto_flag = args.auto_ffmpeg or os.environ.get("AUTO_FFMPEG") == "1"
    if auto_flag:
        print("Auto ffmpeg fetch enabled.")
        if target == "windows":
            fetch_ffmpeg_windows()
        else:
            fetch_ffmpeg_macos()

    if target == "windows" and sys.platform != "win32":
        warn_cross_windows()

    if not args.no_clean:
        clean()

    cmd = _pyinstaller_command() + build_args(
        target=target,
        onefile=args.onefile,
        debug=args.debug,
        console=args.console,
        icon_override=args.icon
    )

    if args.spec_out:
        write_spec(Path(args.spec_out), cmd)
        return

    run(cmd)

    print("\nBuild complete.")
    if target == "macos":
        print(f"macOS app bundle (folder mode): dist/{APP_NAME}/{APP_NAME}.app")
        if args.onefile:
            print("Onefile mode on macOS still produces an .app inside dist/")
    else:
        if args.onefile:
            print(f"Windows single exe: dist/{APP_NAME}.exe")
        else:
            print(f"Windows folder: dist/{APP_NAME}/{APP_NAME}.exe")

    print("\nNext steps:")
    if target == "windows":
        print("  Test on Windows: dist/YouTubeManager/YouTubeManager.exe")
    else:
        print("  Open: dist/YouTubeManager/YouTubeManager.app")

if __name__ == "__main__":
    main()
