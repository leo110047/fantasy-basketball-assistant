from pathlib import Path


def formula_script() -> bytes:
    return (Path(__file__).parent / "static" / "formulas.js").read_bytes()


def court_image() -> bytes:
    return (Path(__file__).parent / "static" / "court.jpg").read_bytes()


def workspace_link_script() -> bytes:
    return (Path(__file__).parent / "static" / "workspace-link.js").read_bytes()
