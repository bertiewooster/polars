from __future__ import annotations

import random
import warnings
from dataclasses import dataclass
from math import isfinite
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Collection, Sequence

from hypothesis.errors import InvalidArgument, NonInteractiveExampleWarning
from hypothesis.strategies import (
    booleans,
    composite,
    lists,
    sampled_from,
)
from hypothesis.strategies._internal.utils import defines_strategy

from polars.dataframe import DataFrame
from polars.datatypes import (
    DTYPE_TEMPORAL_UNITS,
    FLOAT_DTYPES,
    Categorical,
    Datetime,
    Duration,
    is_polars_dtype,
    py_type_to_dtype,
)
from polars.series import Series
from polars.string_cache import StringCache
from polars.testing.asserts import is_categorical_dtype
from polars.testing.parametric.strategies import (
    between,
    scalar_strategies,
)

if TYPE_CHECKING:
    from hypothesis.strategies import DrawFn, SearchStrategy

    from polars.lazyframe import LazyFrame
    from polars.type_aliases import OneOrMoreDataTypes, PolarsDataType


_time_units = list(DTYPE_TEMPORAL_UNITS)


def empty_list(value: Any, nested: bool) -> bool:
    """Check if value is an empty list, or a list that contains only empty lists."""
    if isinstance(value, list):
        return True if value and not nested else all(empty_list(v, True) for v in value)
    return False


# ====================================================================
# Polars 'hypothesis' primitives for Series, DataFrame, and LazyFrame
# See: https://hypothesis.readthedocs.io/
# ====================================================================
MAX_DATA_SIZE = 10  # max generated frame/series length
MAX_COLS = 8  # max number of generated cols

strategy_dtypes = list({dtype.base_type() for dtype in scalar_strategies})


@dataclass
class column:
    """
    Define a column for use with the @dataframes strategy.

    Parameters
    ----------
    name : str
        string column name.
    dtype : PolarsDataType
        a recognised polars dtype.
    strategy : strategy, optional
        supports overriding the default strategy for the given dtype.
    null_probability : float, optional
        percentage chance (expressed between 0.0 => 1.0) that a generated value
        is None. this is applied in addition to any None values output by the
        given/inferred strategy for the column.
    unique : bool, optional
        flag indicating that all values generated for the column should be unique.

    Examples
    --------
    >>> from hypothesis.strategies import sampled_from
    >>> from polars.testing.parametric import column
    >>> column(name="unique_small_ints", dtype=pl.UInt8, unique=True)
    column(name='unique_small_ints', dtype=UInt8, strategy=None, null_probability=None, unique=True)
    >>> column(name="ccy", strategy=sampled_from(["GBP", "EUR", "JPY"]))
    column(name='ccy', dtype=Utf8, strategy=sampled_from(['GBP', 'EUR', 'JPY']), null_probability=None, unique=False)

    """  # noqa: W505

    name: str
    dtype: PolarsDataType | None = None
    strategy: SearchStrategy[Series | int] | None = None
    null_probability: float | None = None
    unique: bool = False

    def __post_init__(self) -> None:
        if (self.null_probability is not None) and (
            self.null_probability < 0 or self.null_probability > 1
        ):
            raise InvalidArgument(
                "null_probability should be between 0.0 and 1.0, or None; found"
                f" {self.null_probability}"
            )

        if self.dtype is None:
            tp = getattr(self.strategy, "_dtype", None)
            if is_polars_dtype(tp):
                self.dtype = tp

        if self.dtype is None and self.strategy is None:
            self.dtype = random.choice(strategy_dtypes)

        elif self.dtype not in scalar_strategies:
            if self.dtype is not None:
                raise InvalidArgument(
                    f"No strategy (currently) available for {self.dtype} type"
                )
            else:
                # given a custom strategy, but no explicit dtype. infer one
                # from the first non-None value that the strategy produces.
                with warnings.catch_warnings():
                    # note: usually you should not call "example()" outside of an
                    # interactive shell, hence the warning. however, here it is
                    # reasonable to do so, so we catch and ignore it
                    warnings.simplefilter("ignore", NonInteractiveExampleWarning)
                    sample_value_iter = (
                        self.strategy.example()  # type: ignore[union-attr]
                        for _ in range(100)
                    )
                    try:
                        sample_value_type = type(
                            next(
                                e
                                for e in sample_value_iter
                                if e is not None and not empty_list(e, nested=True)
                            )
                        )
                    except StopIteration:
                        raise InvalidArgument(
                            "Unable to determine dtype for strategy"
                        ) from None
                if sample_value_type is not None:
                    self.dtype = py_type_to_dtype(sample_value_type)


def columns(
    cols: int | Sequence[str] | None = None,
    *,
    dtype: OneOrMoreDataTypes | None = None,
    min_cols: int | None = 0,
    max_cols: int | None = MAX_COLS,
    unique: bool = False,
) -> list[column]:
    """
    Define multiple columns for use with the @dataframes strategy.

    Generate a fixed sequence of `column` objects suitable for passing to the
    @dataframes strategy, or using standalone (note that this function is not itself
    a strategy).

    Notes
    -----
    Additional control is available by creating a sequence of columns explicitly,
    using the `column` class (an especially useful option is to override the default
    data-generating strategy for a given col/dtype).

    Parameters
    ----------
    cols : {int, [str]}, optional
        integer number of cols to create, or explicit list of column names. if
        omitted a random number of columns (between mincol and max_cols) are
        created.
    dtype : PolarsDataType, optional
        a single dtype for all cols, or list of dtypes (the same length as `cols`).
        if omitted, each generated column is assigned a random dtype.
    min_cols : int, optional
        if not passing an exact size, can set a minimum here (defaults to 0).
    max_cols : int, optional
        if not passing an exact size, can set a maximum value here (defaults to
        MAX_COLS).
    unique : bool, optional
        indicate if the values generated for these columns should be unique
        (per-column).

    Examples
    --------
    >>> from polars.testing.parametric import columns
    >>> from string import punctuation
    >>>
    >>> def test_special_char_colname_init() -> None:
    ...     schema = [(c.name, c.dtype) for c in columns(punctuation)]
    ...     df = pl.DataFrame(schema=schema)
    ...     assert len(cols) == len(df.columns)
    ...     assert 0 == len(df.rows())
    ...
    >>> from polars.testing.parametric import columns
    >>> from hypothesis import given
    >>>
    >>> @given(dataframes(columns(["x", "y", "z"], unique=True)))
    ... def test_unique_xyz(df: pl.DataFrame) -> None:
    ...     assert_something(df)

    """
    # create/assign named columns
    if cols is None:
        cols = random.randint(
            a=min_cols or 0,
            b=max_cols or MAX_COLS,
        )
    if isinstance(cols, int):
        names: list[str] = [f"col{n}" for n in range(cols)]
    else:
        names = list(cols)

    if isinstance(dtype, Sequence):
        if len(dtype) != len(names):
            raise InvalidArgument(f"Given {len(dtype)} dtypes for {len(names)} names")
        dtypes = list(dtype)
    elif dtype is None:
        dtypes = [random.choice(strategy_dtypes) for _ in range(len(names))]
    elif is_polars_dtype(dtype):
        dtypes = [dtype] * len(names)
    else:
        raise InvalidArgument(f"{dtype} is not a valid polars datatype")

    # init list of named/typed columns
    return [column(name=nm, dtype=tp, unique=unique) for nm, tp in zip(names, dtypes)]


@defines_strategy()
def series(
    *,
    name: str | SearchStrategy[str] | None = None,
    dtype: PolarsDataType | None = None,
    size: int | None = None,
    min_size: int | None = 0,
    max_size: int | None = MAX_DATA_SIZE,
    strategy: SearchStrategy[object] | None = None,
    null_probability: float = 0.0,
    allow_infinities: bool = True,
    unique: bool = False,
    chunked: bool | None = None,
    allowed_dtypes: Collection[PolarsDataType] | None = None,
    excluded_dtypes: Collection[PolarsDataType] | None = None,
) -> SearchStrategy[Series]:
    """
    Strategy for producing a polars Series.

    Parameters
    ----------
    name : {str, strategy}, optional
        literal string or a strategy for strings (or None), passed to the Series
        constructor name-param.
    dtype : PolarsDataType, optional
        a valid polars DataType for the resulting series.
    size : int, optional
        if set, creates a Series of exactly this size (ignoring min/max params).
    min_size : int, optional
        if not passing an exact size, can set a minimum here (defaults to 0).
        no-op if `size` is set.
    max_size : int, optional
        if not passing an exact size, can set a maximum value here (defaults to
        MAX_DATA_SIZE). no-op if `size` is set.
    strategy : strategy, optional
        supports overriding the default strategy for the given dtype.
    null_probability : float, optional
        percentage chance (expressed between 0.0 => 1.0) that a generated value is
        None. this is applied independently of any None values generated by the
        underlying strategy.
    allow_infinities : bool, optional
        optionally disallow generation of +/-inf values for floating-point dtypes.
    unique : bool, optional
        indicate whether Series values should all be distinct.
    chunked : bool, optional
        ensure that Series with more than one element have ``n_chunks`` > 1.
        if omitted, chunking is applied at random.
    allowed_dtypes : {list,set}, optional
        when automatically generating Series data, allow only these dtypes.
    excluded_dtypes : {list,set}, optional
        when automatically generating Series data, exclude these dtypes.

    Notes
    -----
    In actual usage this is deployed as a unit test decorator, providing a strategy
    that generates multiple Series with the given dtype/size characteristics for the
    unit test. While developing a strategy/test, it can also be useful to call
    `.example()` directly on a given strategy to see concrete instances of the
    generated data.

    Examples
    --------
    >>> from polars.testing.parametric import series
    >>> from hypothesis import given
    >>>
    >>> @given(df=series())
    ... def test_repr(s: pl.Series) -> None:
    ...     assert isinstance(repr(s), str)
    >>>
    >>> s = series(dtype=pl.Int32, max_size=5)
    >>> s.example()  # doctest: +SKIP
    shape: (4,)
    Series: '' [i64]
    [
        54666
        -35
        6414
        -63290
    ]

    """
    selectable_dtypes = [
        dtype
        for dtype in (allowed_dtypes or strategy_dtypes)
        if dtype not in (excluded_dtypes or ())
    ]
    if null_probability and (null_probability < 0 or null_probability > 1):
        raise InvalidArgument(
            "null_probability should be between 0.0 and 1.0; found"
            f" {null_probability}"
        )
    null_probability = float(null_probability or 0.0)

    @composite
    def draw_series(draw: DrawFn) -> Series:
        with StringCache():
            # create/assign series dtype and retrieve matching strategy
            series_dtype = (
                draw(sampled_from(selectable_dtypes)) if dtype is None else dtype
            )
            if strategy is None:
                if series_dtype is Datetime or series_dtype is Duration:
                    series_dtype = series_dtype(random.choice(_time_units))  # type: ignore[operator]
                dtype_strategy = scalar_strategies[
                    series_dtype
                    if series_dtype in scalar_strategies
                    else series_dtype.base_type()
                ]
            else:
                dtype_strategy = strategy

            if series_dtype in FLOAT_DTYPES and not allow_infinities:
                dtype_strategy = dtype_strategy.filter(
                    lambda x: not isinstance(x, float) or isfinite(x)
                )

            # create/assign series size
            series_size = (
                between(
                    draw, int, min_=(min_size or 0), max_=(max_size or MAX_DATA_SIZE)
                )
                if size is None
                else size
            )
            # assign series name
            series_name = name if isinstance(name, str) or name is None else draw(name)

            # create series using dtype-specific strategy to generate values
            if series_size == 0:
                series_values = []
            elif null_probability == 1:
                series_values = [None] * series_size
            else:
                series_values = draw(
                    lists(
                        dtype_strategy,
                        min_size=series_size,
                        max_size=series_size,
                        unique=unique,
                    )
                )

            # apply null values (custom frequency)
            if null_probability and null_probability != 1:
                for idx in range(series_size):
                    if random.random() < null_probability:
                        series_values[idx] = None

            # init series with strategy-generated data
            s = Series(
                name=series_name,
                dtype=series_dtype,
                values=series_values,
            )
            if is_categorical_dtype(dtype):
                s = s.cast(Categorical)
            if series_size and (chunked or (chunked is None and draw(booleans()))):
                split_at = series_size // 2
                s = s[:split_at].append(s[split_at:], append_chunks=True)
            return s

    return draw_series()


_failed_frame_init_msgs_: set[str] = set()


@defines_strategy()
def dataframes(
    cols: int | column | Sequence[column] | None = None,
    lazy: bool = False,
    *,
    min_cols: int | None = 0,
    max_cols: int | None = MAX_COLS,
    size: int | None = None,
    min_size: int | None = 0,
    max_size: int | None = MAX_DATA_SIZE,
    chunked: bool | None = None,
    include_cols: Sequence[column] | None = None,
    null_probability: float | dict[str, float] = 0.0,
    allow_infinities: bool = True,
    allowed_dtypes: Collection[PolarsDataType] | None = None,
    excluded_dtypes: Collection[PolarsDataType] | None = None,
) -> SearchStrategy[DataFrame | LazyFrame]:
    """
    Provides a strategy for producing a DataFrame or LazyFrame.

    Parameters
    ----------
    cols : {int, columns}, optional
        integer number of columns to create, or a sequence of `column` objects
        that describe the desired DataFrame column data.
    lazy : bool, optional
        produce a LazyFrame instead of a DataFrame.
    min_cols : int, optional
        if not passing an exact size, can set a minimum here (defaults to 0).
    max_cols : int, optional
        if not passing an exact size, can set a maximum value here (defaults to
        MAX_COLS).
    size : int, optional
        if set, will create a DataFrame of exactly this size (and ignore min/max len
        params).
    min_size : int, optional
        if not passing an exact size, set the minimum number of rows in the
        DataFrame.
    max_size : int, optional
        if not passing an exact size, set the maximum number of rows in the
        DataFrame.
    chunked : bool, optional
        ensure that DataFrames with more than row have ``n_chunks`` > 1. if
        omitted, chunking will be randomised at the level of individual Series.
    include_cols : [column], optional
        a list of `column` objects to include in the generated DataFrame. note that
        explicitly provided columns are appended onto the list of existing columns
        (if any present).
    null_probability : {float, dict[str,float]}, optional
        percentage chance (expressed between 0.0 => 1.0) that a generated value is
        None. this is applied independently of any None values generated by the
        underlying strategy, and can be applied either on a per-column basis (if
        given as a ``{col:pct}`` dict), or globally. if null_probability is defined
        on a column, it takes precedence over the global value.
    allow_infinities : bool, optional
        optionally disallow generation of +/-inf values for floating-point dtypes.
    allowed_dtypes : {list,set}, optional
        when automatically generating data, allow only these dtypes.
    excluded_dtypes : {list,set}, optional
        when automatically generating data, exclude these dtypes.

    Notes
    -----
    In actual usage this is deployed as a unit test decorator, providing a strategy
    that generates DataFrames or LazyFrames with the given characteristics for
    the unit test. While developing a strategy/test, it can also be useful to
    call `.example()` directly on a given strategy to see concrete instances of
    the generated data.

    Examples
    --------
    Use `column` or `columns` to specify the schema of the types of DataFrame to
    generate. Note: in actual use the strategy is applied as a test decorator, not
    used standalone.

    >>> from polars.testing.parametric import column, columns, dataframes
    >>> from hypothesis import given
    >>>
    >>> # generate arbitrary DataFrames
    >>> @given(df=dataframes())
    ... def test_repr(df: pl.DataFrame) -> None:
    ...     assert isinstance(repr(df), str)
    >>>
    >>> # generate LazyFrames with at least 1 column, random dtypes, and specific size:
    >>> df = dataframes(min_cols=1, lazy=True, max_size=5)
    >>> df.example()  # doctest: +SKIP
    >>>
    >>> # generate DataFrames with known colnames, random dtypes (per test, not per-frame):
    >>> df_strategy = dataframes(columns(["x", "y", "z"]))
    >>> df.example()  # doctest: +SKIP
    >>>
    >>> # generate frames with explicitly named/typed columns and a fixed size:
    >>> df_strategy = dataframes(
    ...     [
    ...         column("x", dtype=pl.Int32),
    ...         column("y", dtype=pl.Float64),
    ...     ],
    ...     size=2,
    ... )
    >>> df_strategy.example()  # doctest: +SKIP
    shape: (2, 2)
    ┌───────────┬────────────┐
    │ x         ┆ y          │
    │ ---       ┆ ---        │
    │ i32       ┆ f64        │
    ╞═══════════╪════════════╡
    │ -15836    ┆ 1.1755e-38 │
    │ 575050513 ┆ NaN        │
    └───────────┴────────────┘
    """  # noqa: 501
    _failed_frame_init_msgs_.clear()

    if isinstance(min_size, int) and min_cols in (0, None):
        min_cols = 1

    selectable_dtypes = [
        dtype
        for dtype in (allowed_dtypes or strategy_dtypes)
        if dtype not in (excluded_dtypes or ())
    ]

    @composite
    def draw_frames(draw: DrawFn) -> DataFrame | LazyFrame:
        """Reproducibly generate random DataFrames according to the given spec."""
        with StringCache():
            # if not given, create 'n' cols with random dtypes
            if cols is None or isinstance(cols, int):
                n = cols or between(
                    draw, int, min_=(min_cols or 0), max_=(max_cols or MAX_COLS)
                )
                dtypes_ = [draw(sampled_from(selectable_dtypes)) for _ in range(n)]
                coldefs = columns(cols=n, dtype=dtypes_)
            elif isinstance(cols, column):
                coldefs = [cols]
            else:
                coldefs = list(cols)

            # append any explicitly provided cols
            coldefs.extend(include_cols or ())

            # assign dataframe/series size
            series_size = (
                between(
                    draw, int, min_=(min_size or 0), max_=(max_size or MAX_DATA_SIZE)
                )
                if size is None
                else size
            )

            # assign names, null probability
            for idx, c in enumerate(coldefs):
                if c.name is None:
                    c.name = f"col{idx}"
                if c.null_probability is None:
                    if isinstance(null_probability, dict):
                        c.null_probability = null_probability.get(c.name, 0.0)
                    else:
                        c.null_probability = null_probability

            # init dataframe from generated series data; series data is
            # given as a python-native sequence.
            data = {
                c.name: draw(
                    series(
                        name=c.name,
                        dtype=c.dtype,
                        size=series_size,
                        null_probability=(c.null_probability or 0.0),
                        allow_infinities=allow_infinities,
                        strategy=c.strategy,
                        unique=c.unique,
                        chunked=(chunked is None and draw(booleans())),
                    )
                )
                for c in coldefs
            }

            # note: randomly change between column-wise and row-wise frame init
            orient = "col"
            if draw(booleans()):
                data = list(zip(*data.values()))  # type: ignore[assignment]
                orient = "row"

            schema = [(c.name, c.dtype) for c in coldefs]
            try:
                df = DataFrame(data=data, schema=schema, orient=orient)  # type: ignore[arg-type]

                # optionally generate chunked frames
                if series_size > 1 and chunked is True:
                    split_at = series_size // 2
                    df = df[:split_at].vstack(df[split_at:])

                _failed_frame_init_msgs_.clear()
                return df.lazy() if lazy else df

            except Exception:
                # print code that will allow any init failure to be reproduced
                failed_frame_init = dedent(
                    f"""
                    # failed frame init: reproduce with...
                    pl.DataFrame(
                        data={data!r},
                        schema={repr(schema).replace("', ","', pl.")},
                        orient={orient!r},
                    )
                    """.replace(
                        "datetime.", ""
                    )
                )
                # note: this avoids printing the repro twice
                if failed_frame_init not in _failed_frame_init_msgs_:
                    _failed_frame_init_msgs_.add(failed_frame_init)
                    print(failed_frame_init)
                raise

    return draw_frames()