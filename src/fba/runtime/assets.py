from pathlib import Path


def formula_script() -> bytes:
    return (Path(__file__).parent / "static" / "formulas.js").read_bytes()
