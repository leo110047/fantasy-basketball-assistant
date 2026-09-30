"""Executable vector equations with the same input/result evidence as scalar equations."""

from collections.abc import Callable
from typing import NamedTuple

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.formula import ArrayFormulaTrace, FormulaDefinition, NumericValue
from fba.formulas import vector
from fba.formulas.vector import Array, Inputs


class ArrayFormula(NamedTuple):
    id: str
    name: str
    latex: str
    units: str
    parameters: tuple[str, ...]
    implementation: Callable[[Inputs], Array]
    example: dict[str, NumericValue]
    input_units: dict[str, str]


ARRAY_FORMULAS = (
    ArrayFormula(
        "row_rates",
        "逐場每分鐘產出",
        r"r_{is}=C_{is}/M_i",
        "count per minute",
        (),
        vector.row_rates,
        {"counts": ((10.0, 4.0), (6.0, 2.0)), "minutes": (20.0, 10.0)},
        {"counts": "count", "minutes": "minutes"},
    ),
    ArrayFormula(
        "matrix_product",
        "線性組合",
        r"Y=AB",
        "combined units",
        (),
        vector.matrix_product,
        {"left": ((1.0, 2.0),), "right": (3.0, 4.0)},
        {"left": "left units", "right": "right units"},
    ),
    ArrayFormula(
        "bernoulli_mean",
        "出賽抽樣均值",
        r"E[IX]=q\mu",
        "statistic units",
        (),
        vector.bernoulli_mean,
        {"means": ((2.0, 4.0),), "probability": (0.5,)},
        {"means": "statistic units", "probability": "probability"},
    ),
    ArrayFormula(
        "bernoulli_covariance",
        "出賽抽樣共變異數",
        r"\operatorname{Cov}(IX)=qC+q(1-q)\mu\mu^T",
        "squared statistic units",
        (),
        vector.bernoulli_covariance,
        {"means": ((2.0, 4.0),), "probability": (0.5,), "covariance": (((2.0, 0.0), (0.0, 2.0)),)},
        {
            "means": "statistic units",
            "probability": "probability",
            "covariance": "squared statistic units",
        },
    ),
    ArrayFormula(
        "availability_value",
        "健康條件下的效用",
        r"v=u/\max(q,q_{min})",
        "utility per available game",
        ("fit.availability_floor",),
        vector.availability_value,
        {"priority": (2.0, 4.0), "availability": (0.5, 0.0), "floor": 0.25},
        {"priority": "utility", "availability": "probability", "floor": "probability"},
    ),
    ArrayFormula(
        "sample_covariance",
        "樣本共變異數",
        r"C=\sum_i(x_i-\bar x)(x_i-\bar x)^T/\max(1,n-1)",
        "squared statistic units",
        (),
        vector.sample_covariance,
        {"values": ((1.0, 2.0), (3.0, 4.0))},
        {"values": "statistic units"},
    ),
    ArrayFormula(
        "control_variate",
        "控制變量修正",
        r"X'=X-(C-E[C])",
        "statistic units",
        (),
        vector.control_variate,
        {"physical": (5.0, 7.0), "realized": (3.0, 4.0), "expected": (2.0, 2.0)},
        {
            "physical": "statistic units",
            "realized": "statistic units",
            "expected": "statistic units",
        },
    ),
    ArrayFormula(
        "stratified_uniform",
        "分層均勻抽樣",
        r"U_i=(i+J_i)/n,\quad J_i\in[0,1)",
        "probability coordinate",
        ("fit.health_samples",),
        vector.stratified_uniform,
        {"jitter": ((0.5, 0.0), (0.5, 0.0))},
        {"jitter": "uniform coordinate"},
    ),
    ArrayFormula(
        "standard_deviation_floor",
        "有限差分步長",
        r"h=\max(\alpha\,\operatorname{std}(X),h_{min})",
        "input units",
        ("fit.gradient_fraction", "fit.gradient_floor"),
        vector.standard_deviation_floor,
        {"values": ((1.0, 2.0), (3.0, 6.0)), "fraction": 0.5, "floor": 0.75},
        {"values": "input units", "fraction": "fraction", "floor": "input units"},
    ),
    ArrayFormula(
        "central_difference",
        "中央差分斜率",
        r"f'(x)\approx[f(x+h)-f(x-h)]/(2h)",
        "output units per input unit",
        ("fit.gradient_fraction", "fit.gradient_floor"),
        vector.central_difference,
        {"plus": (3.0, 8.0), "minus": (1.0, 2.0), "step": (1.0, 2.0)},
        {"plus": "output units", "minus": "output units", "step": "input units"},
    ),
    ArrayFormula(
        "utility_rescale",
        "管理效用尺度轉換",
        r"u'=u\,\operatorname{std}(v)/s",
        "reference utility",
        (),
        vector.utility_rescale,
        {"values": (2.0, 4.0), "reference": (1.0, 5.0), "scale": 2.0},
        {"values": "managed utility", "reference": "reference utility", "scale": "managed utility"},
    ),
    ArrayFormula(
        "average_ranks",
        "同分平均排名",
        r"R_i=(a_i+b_i)/2\quad\text{zero-based equal-value rank interval }[a_i,b_i]",
        "rank",
        (),
        vector.average_ranks,
        {"values": (3.0, 1.0, 1.0)},
        {"values": "ranking values"},
    ),
    ArrayFormula(
        "calibrate_distribution",
        "逐場聯合分布均值與上下限校準",
        r"X_s=H_s\mu_s/\bar H_s;\quad X_c=\min(X_p,\lambda w_c),\quad "
        r"w_c=X_p(H_c+a)/(H_p+b),\quad X_{score}=\sum_s\alpha_sX_s",
        "count per game",
        (
            "projection.nested_counts",
            "projection.scoring_terms",
            "projection.count_pseudocount",
            "projection.attempt_pseudocount",
            "projection.feasibility_tolerance",
            "projection.search_iterations",
        ),
        vector.calibrate_distribution,
        {
            "history": ((1.0, 2.0, 3.0), (2.0, 4.0, 6.0)),
            "target": (1.5, 3.0, 4.5),
            "nested": ((0.0, 1.0),),
            "scoring_axis": 2.0,
            "scoring_terms": ((0.0, 1.0), (1.0, 1.0)),
            "count_pseudocount": 1.0,
            "attempt_pseudocount": 2.0,
            "tolerance": 1e-9,
            "iterations": 60.0,
        },
        {
            "history": "count per game",
            "target": "count per game",
            "nested": "child/parent axis indices",
            "scoring_axis": "axis index",
            "scoring_terms": "axis index/coefficient",
            "count_pseudocount": "count",
            "attempt_pseudocount": "count",
            "tolerance": "count",
            "iterations": "iterations",
        },
    ),
    ArrayFormula(
        "rounding_distribution",
        "共享均勻變數的離散分布",
        r"X_s=\lfloor x_s\rfloor+\mathbf1[U_g<\{x_s\}],\quad U_g\sim U[0,1];\quad "
        r"P(X)=\prod_g(u_g-l_g)/n",
        "statistic counts; final column is probability mass",
        ("projection.rounding_groups", "projection.scoring_terms"),
        vector.rounding_distribution,
        {
            "values": ((0.5, 1.5, 2.0),),
            "groups": ((1.0, 1.0, 0.0),),
            "scoring_axis": 2.0,
            "scoring_terms": ((0.0, 1.0), (1.0, 1.0)),
        },
        {
            "values": "count per game",
            "groups": "binary group membership",
            "scoring_axis": "axis index",
            "scoring_terms": "axis index/coefficient",
        },
    ),
    ArrayFormula(
        "bounded_mean",
        "有上限的樣本均值校準",
        r"x_i=\min(c_i,\lambda w_i),\quad \operatorname{mean}(x)=\mu",
        "count per game",
        ("projection.feasibility_tolerance", "projection.search_iterations"),
        vector.bounded_mean,
        {
            "capacity": (2.0, 4.0),
            "weights": (1.0, 1.0),
            "target": 2.0,
            "tolerance": 1e-9,
            "iterations": 60.0,
        },
        {
            "capacity": "count per game",
            "weights": "relative weight",
            "target": "count per game",
            "tolerance": "count per game",
            "iterations": "bisection iterations",
        },
    ),
    ArrayFormula(
        "weighted_rows",
        "分布第一動差",
        r"\mu=\sum_i w_i x_i",
        "statistic units",
        (),
        vector.weighted_rows,
        {"values": ((1.0, 2.0), (3.0, 4.0)), "weights": (0.5, 0.5)},
        {"values": "statistic units", "weights": "probability mass"},
    ),
    ArrayFormula(
        "weighted_second",
        "分布第二動差",
        r"M=\sum_i w_i x_i x_i^T",
        "squared statistic units",
        (),
        vector.weighted_second,
        {"values": ((1.0, 2.0), (3.0, 4.0)), "weights": (0.5, 0.5)},
        {"values": "statistic units", "weights": "probability mass"},
    ),
    ArrayFormula(
        "centered_covariance",
        "動差轉共變異數",
        r"C=M-\mu\mu^T",
        "squared statistic units",
        (),
        vector.centered_covariance,
        {"mean": (2.0, 3.0), "second": ((5.0, 7.0), (7.0, 10.0))},
        {"mean": "statistic units", "second": "squared statistic units"},
    ),
    ArrayFormula(
        "category_points",
        "類別勝負與平手計分",
        r"s_c=\mathbf1[d_c>0]+t_c\mathbf1[d_c=0]",
        "category score",
        (),
        vector.category_points,
        {"differences": ((1.0, 0.0, -1.0), (-1.0, 1.0, 0.0)), "ties": (0.5, 0.5, 0.5)},
        {"differences": "directed category units", "ties": "tie score"},
    ),
    ArrayFormula(
        "week_points",
        "整週 One Win 計分",
        r"s=\mathbf1[\sum s_c>\sum o_c]+t_w\mathbf1[\sum s_c=\sum o_c]",
        "week score",
        (),
        vector.week_points,
        {
            "differences": ((1.0, 0.0, -1.0), (1.0, 1.0, -1.0)),
            "ties": (0.5, 0.5, 0.5),
            "week_tie": 0.5,
        },
        {
            "differences": "directed category units",
            "ties": "category tie score",
            "week_tie": "week tie score",
        },
    ),
    ArrayFormula(
        "bid_distribution",
        "離散上限競價累積分布",
        r"F_j(b)=P[\min(M_j,m+\lfloor V_j/\delta\rfloor\delta)\leq b],\quad "
        r"\log V_j\sim N(\log p_j-a,\sigma^2)",
        "probability",
        ("market.volatility",),
        vector.bid_distribution,
        {
            "levels": (1.0, 2.0, 3.0),
            "maximum": (3.0, 3.0),
            "premium": (1.0, 2.0),
            "volatility": 0.0,
            "shift": 0.0,
            "minimum": 1.0,
            "increment": 1.0,
        },
        {
            "levels": "currency",
            "maximum": "currency",
            "premium": "currency",
            "volatility": "log bid standard deviation",
            "shift": "log bid multiplier",
            "minimum": "currency",
            "increment": "currency",
        },
    ),
    ArrayFormula(
        "second_bid_survival",
        "成交價超越機率",
        r"P(S>b)=1-\prod_j F_j(b)-\sum_j[1-F_j(b)]\prod_{k\ne j}F_k(b-\delta)",
        "probability",
        (),
        vector.second_bid_survival,
        {"cdf": ((0.5, 1.0), (0.5, 1.0)), "previous": ((0.0, 0.5), (0.0, 0.5))},
        {"cdf": "bid cumulative probability", "previous": "previous increment probability"},
    ),
    ArrayFormula(
        "health_transitions",
        "健康與傷停轉移率",
        r"b=\min(1/m,q/\max(1-q,\epsilon)),\quad h=(1-q)b/\max(q,\epsilon)",
        "transition probability",
        ("fit.mean_missed_games",),
        vector.health_transitions,
        {"availability": (0.5, 0.8), "mean_missed": 2.0, "floor": 1e-12},
        {"availability": "probability", "mean_missed": "games", "floor": "numerical floor"},
    ),
    ArrayFormula(
        "conditional_health",
        "條件式未來健康機率",
        r"P(H_g=1\mid H_0=s)=\operatorname{clip}_{[0,1]}[q+(s-q)d_g]",
        "probability",
        (),
        vector.conditional_health,
        {"availability": (0.5, 0.8), "status": 1.0, "decay": (0.5, 0.25)},
        {"availability": "probability", "status": "binary health", "decay": "correlation"},
    ),
    ArrayFormula(
        "health_count_covariance",
        "相關健康場次共變異數",
        r"C=q(1-q)\sum_{ij}d_{|g_i-g_j|}\,rr^T",
        "squared statistic units",
        (),
        vector.health_count_covariance,
        {"availability": 0.5, "decay": ((1.0, 0.5), (0.5, 1.0)), "stats": (2.0, 4.0)},
        {"availability": "probability", "decay": "correlation", "stats": "statistic units"},
    ),
    ArrayFormula(
        "managed_covariance",
        "管理後陣容共變異數",
        r"C=\sum_n E[N_n]C_n+\sum_h(X_h-\bar X)(X_h-\bar X)^T/\max(1,H-1)+C_{control}",
        "squared statistic units",
        (),
        vector.managed_covariance,
        {
            "counts": ((((1.0,),),), (((1.0,),),)),
            "covariance": (((2.0,),),),
            "physical": ((((1.0,),),), (((3.0,),),)),
            "correction": ((((1.0,),),),),
        },
        {
            "counts": "counted games",
            "covariance": "squared statistic units",
            "physical": "statistic units",
            "correction": "squared statistic units",
        },
    ),
    ArrayFormula(
        "utility_blend",
        "陣容管理效用混合",
        r"v=(1-\alpha)v_0+\alpha v_{managed}",
        "utility",
        ("fit.steps",),
        vector.utility_blend,
        {"baseline": (2.0, 4.0), "managed": (4.0, 0.0), "step": 0.25},
        {"baseline": "utility", "managed": "utility", "step": "mixing weight"},
    ),
    ArrayFormula(
        "covariance_root",
        "共變異數平方根",
        r"Q\sqrt{\max(\Lambda,0)}Q^T,\quad Q\Lambda Q^T=(C+C^T)/2",
        "statistic units",
        (),
        vector.covariance_root,
        {"covariance": ((4.0, 0.0), (0.0, 9.0))},
        {"covariance": "squared statistic units"},
    ),
    ArrayFormula(
        "soft_majority",
        "陣容多數類別分數",
        r"\sigma(d_{(\lfloor(K-1)/2\rfloor)}/h)",
        "probability",
        ("fit.bandwidth",),
        vector.soft_majority,
        {"differences": ((-1.0, 0.0, 1.0), (0.0, 0.0, 0.0)), "bandwidth": 1.0},
        {"differences": "standardized category margins", "bandwidth": "standardized margin units"},
    ),
    ArrayFormula(
        "category_marginals",
        "類別領先比例與邊際權重",
        r"L_c=\operatorname{mean}(d_c>0),\quad "
        r"g_c=[\bar S(d+\epsilon e_c)-\bar S(d-\epsilon e_c)]/(2\epsilon),\quad W_c=Kg_c/\sum_jg_j",
        "lead probability and relative weight",
        ("fit.bandwidth", "fit.gradient_fraction"),
        vector.category_marginals,
        {"differences": ((0.0, 0.0), (0.0, 0.0)), "bandwidth": 1.0, "step": 0.1},
        {
            "differences": "standardized category margins",
            "bandwidth": "standardized margin units",
            "step": "standardized margin units",
        },
    ),
    ArrayFormula(
        "bootstrap_scale",
        "逐場樣本均值校準",
        r"X'_{is}=X_{is}\mu_s/\bar H_s;\quad "
        r"\bar H_s=0\Rightarrow X'_{is}=Z_{is},\ Z_{is}\sim Poisson(\mu_s)",
        "count per game",
        (),
        vector.bootstrap_scale,
        {
            "history": ((2.0, 0.0), (4.0, 0.0)),
            "samples": ((3.0, 0.0),),
            "target": (6.0, 2.0),
            "zero_mean_draws": ((0.0, 3.0),),
        },
        {
            "history": "historical count per game",
            "samples": "sampled count per game",
            "target": "expected count per game",
            "zero_mean_draws": "Poisson count per game",
        },
    ),
    ArrayFormula(
        "sampling_covariance",
        "健康情境合併共變異數",
        r"C=E[C_h]+E[(\mu_h-E[\mu_h])(\mu_h-E[\mu_h])^T]",
        "squared statistic units",
        (),
        vector.sampling_covariance,
        {
            "means": ((1.0, 2.0), (3.0, 4.0)),
            "covariances": (((2.0, 0.0), (0.0, 2.0)), ((2.0, 0.0), (0.0, 2.0))),
        },
        {"means": "statistic units", "covariances": "squared statistic units"},
    ),
    ArrayFormula(
        "standardized_margins",
        "標準化對手差距",
        r"d=(x-o)/s",
        "standardized category margins",
        (),
        vector.standardized_margins,
        {"values": ((3.0, 5.0),), "opponent": (1.0, 1.0), "scale": (2.0, 2.0)},
        {"values": "category units", "opponent": "category units", "scale": "category units"},
    ),
    ArrayFormula(
        "logistic_objective",
        "接受模型擬合損失",
        r"L(\beta)=\operatorname{mean}(\log(1+e^{X\beta})-yX\beta)",
        "negative log likelihood",
        (),
        vector.logistic_objective,
        {"features": ((1.0,), (2.0,)), "coefficients": (0.0,), "outcomes": (0.0, 1.0)},
        {
            "features": "model feature units",
            "coefficients": "inverse feature units",
            "outcomes": "binary acceptance",
        },
    ),
    ArrayFormula(
        "logistic_gradient",
        "接受模型損失梯度",
        r"\nabla L=X^T(\sigma(X\beta)-y)/n",
        "negative log likelihood per coefficient",
        (),
        vector.logistic_gradient,
        {"features": ((1.0,), (2.0,)), "coefficients": (0.0,), "outcomes": (0.0, 1.0)},
        {
            "features": "model feature units",
            "coefficients": "inverse feature units",
            "outcomes": "binary acceptance",
        },
    ),
    ArrayFormula(
        "logistic_hessian",
        "接受模型損失曲率",
        r"H=X^T\operatorname{diag}(p(1-p))X/n",
        "negative log likelihood per squared coefficient",
        (),
        vector.logistic_hessian,
        {"features": ((1.0,), (2.0,)), "coefficients": (0.0,)},
        {"features": "model feature units", "coefficients": "inverse feature units"},
    ),
    ArrayFormula(
        "calibration_fit",
        "機率收縮係數擬合",
        r"c=\operatorname{clip}_{[0,1]}\frac{\sum(p-.5)(y-.5)}{\sum(p-.5)^2}",
        "coefficient",
        ("calibration",),
        vector.calibration_fit,
        {"predicted": (0.75, 0.25), "observed": (0.625, 0.375)},
        {"predicted": "probability", "observed": "outcome score"},
    ),
)


class ArrayCalculation(NamedTuple):
    result: Array
    formula_id: str
    inputs: tuple[tuple[str, Array], ...]

    @property
    def trace(self) -> ArrayFormulaTrace:
        # Keep immutable NumPy evidence on the search path; serialize only when
        # exposing it, without materializing Python tuples for every candidate.
        return ArrayFormulaTrace(
            formula_id=self.formula_id,
            inputs={k: numeric(v) for k, v in self.inputs},
            result=numeric(self.result),
        )


def numeric(value: Array) -> NumericValue:
    return float(value) if value.ndim == 0 else tuple(numeric(row) for row in value)


def evaluate_array(formula_id: str, **inputs: Array | float) -> ArrayCalculation:
    definition = next((f for f in ARRAY_FORMULAS if f.id == formula_id), None)
    if definition is None or set(inputs) != set(definition.input_units):
        raise DataError(f"formula.{formula_id}: unknown formula or incompatible inputs")
    arrays = {k: np.array(v, dtype=np.float64, copy=True) for k, v in inputs.items()}
    if any(not np.isfinite(v).all() for v in arrays.values()):
        raise DataError(f"formula.{formula_id}: non-finite input")
    for value in arrays.values():
        value.flags.writeable = False
    try:
        result = np.asarray(definition.implementation(arrays), dtype=np.float64)
    except DataError:
        raise
    except (ValueError, IndexError, np.linalg.LinAlgError):
        raise DataError(f"formula.{formula_id}: incompatible numeric shape or domain") from None
    if not np.isfinite(result).all():
        raise DataError(f"formula.{formula_id}: non-finite result")
    result.flags.writeable = False
    return ArrayCalculation(result, formula_id, tuple(arrays.items()))


def array_definitions() -> tuple[FormulaDefinition, ...]:
    return tuple(
        FormulaDefinition(
            id=f.id,
            name=f.name,
            latex=f.latex,
            units=f.units,
            parameters=f.parameters,
            input_units=f.input_units,
            example=evaluate_array(
                f.id, **{k: np.asarray(v, dtype=np.float64) for k, v in f.example.items()}
            ).trace,
        )
        for f in ARRAY_FORMULAS
    )
