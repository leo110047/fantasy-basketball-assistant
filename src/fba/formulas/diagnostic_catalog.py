"""Registered statistical diagnostics used by the in-season model."""

from fba.contracts.formula import ScalarFormula
from fba.formulas import scalar

FORMULAS = (
    ScalarFormula(
        "empirical_error",
        "觀測樣本平均值標準誤",
        r"SE=\sqrt{\sum_i(x_i-\bar x)^2/[n(n-1)]}",
        "observation units",
        (),
        scalar.empirical_error,
        {"values": (2.0, 4.0)},
        {"values": "observation units"},
    ),
    ScalarFormula(
        "effective_samples",
        "加權週群集的有效樣本數",
        r"n_{eff}=(\sum w_i)^2/\sum w_i^2",
        "independent clusters",
        (),
        scalar.effective_samples,
        {"weights": (1.0, 3.0)},
        {"weights": "observations per matchup-week"},
    ),
    ScalarFormula(
        "monitor_margin",
        "有界結果的校準監控誤差範圍",
        r"u=z/(2\sqrt n)",
        "probability",
        ("calibration_confidence_z",),
        scalar.monitor_margin,
        {"samples": 100.0, "z": 1.96},
        {"samples": "effective independent matchup-week clusters", "z": "standard deviations"},
    ),
    ScalarFormula(
        "exposure_rate",
        "總量每分鐘產出",
        r"r=\sum C_i/\sum M_i",
        "count/minute",
        (),
        scalar.exposure_rate,
        {"counts": (1.0, 9.0), "exposure": (1.0, 9.0)},
        {"counts": "count/game", "exposure": "minutes/game"},
    ),
    ScalarFormula(
        "exposure_error",
        "按場分群且含 Poisson 下限的產出標準誤",
        r"SE=\sqrt{\max\{\frac{n\sum(C_i-rM_i)^2}{(n-1)(\sum M_i)^2},"
        r"\frac{\max(r,r_{model})}{\sum M_i}\}}",
        "count/minute",
        ("production_sigma",),
        scalar.exposure_error,
        {"counts": (1.0, 9.0), "exposure": (1.0, 9.0), "model": 1.0},
        {"counts": "count/game", "exposure": "minutes/game", "model": "count/minute"},
    ),
)
