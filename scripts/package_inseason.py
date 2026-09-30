"""Build a self-contained in-season launcher; never publishes or installs it."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist/inseason"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "FantasyBasketballInseason",
        "--distpath",
        str(output),
        "--workpath",
        str(output / "work"),
        "--specpath",
        str(output / "work"),
        "--paths",
        str(root / "src"),
        "--collect-data",
        "tzdata",
        "--collect-submodules",
        "keyring.backends",
        "--add-data",
        f"{root / 'src/fba/apps/inseason/static'}:fba/apps/inseason/static",
        "--add-data",
        f"{root / 'src/fba/apps/inseason/defaults'}:fba/apps/inseason/defaults",
        "--add-data",
        f"{root / 'src/fba/runtime/static'}:fba/runtime/static",
        str(root / "src/fba/apps/inseason/launcher.py"),
    ]
    if sys.platform == "darwin":
        command.insert(3, "--windowed")
    subprocess.run(command, check=True)
    if sys.platform == "darwin":
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-ov",
                "-format",
                "UDZO",
                "-volname",
                "季賽助手",
                "-srcfolder",
                str(output / "FantasyBasketballInseason.app"),
                str(output / "FantasyBasketballInseason-macOS.dmg"),
            ],
            check=True,
        )
    elif sys.platform == "win32":
        # Inno Setup is preinstalled on the GitHub Windows runner. The installer
        # bundles the interpreter and creates a normal per-user Start Menu shortcut.
        compiler = shutil.which("iscc")
        if compiler is None:
            compiler_path = Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe")
            if not compiler_path.is_file():
                raise RuntimeError("Inno Setup 6 is required to build the Windows installer")
            compiler = str(compiler_path)
        script = output / "installer.iss"
        script.write_text(
            "[Setup]\nAppName=Fantasy Basketball Inseason\nAppVersion=0.1.0\n"
            "DefaultDirName={localappdata}\\Programs\\FantasyBasketballInseason\n"
            "PrivilegesRequired=lowest\nUninstallDisplayName=Fantasy Basketball Inseason\n"
            f"OutputDir={output}\nOutputBaseFilename=FantasyBasketballInseason-Windows\n"
            "[Files]\n"
            f'Source: "{output / "FantasyBasketballInseason"}\\*"; DestDir: "{{app}}"; '
            "Flags: recursesubdirs createallsubdirs\n"
            '[Icons]\nName: "{userprograms}\\Fantasy Basketball Inseason"; '
            'Filename: "{app}\\FantasyBasketballInseason.exe"\n',
            encoding="utf-8-sig",
        )
        subprocess.run([compiler, str(script)], check=True)
    else:
        raise RuntimeError("Installer builds require macOS or Windows")


if __name__ == "__main__":
    main()
