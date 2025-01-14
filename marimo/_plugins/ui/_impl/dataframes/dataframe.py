# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Final,
    List,
    Optional,
    Union,
)

from marimo._output.rich_help import mddoc
from marimo._plugins.ui._core.ui_element import UIElement
from marimo._plugins.ui._impl.dataframes.transforms.apply import (
    TransformsContainer,
    get_handler_for_dataframe,
)
from marimo._plugins.ui._impl.dataframes.transforms.types import (
    DataFrameType,
    Transformations,
)
from marimo._plugins.ui._impl.table import (
    SearchTableArgs,
    SearchTableResponse,
    SortArgs,
)
from marimo._plugins.ui._impl.tables.table_manager import (
    FieldTypes,
    TableManager,
)
from marimo._plugins.ui._impl.tables.utils import (
    get_table_manager,
)
from marimo._runtime.functions import EmptyArgs, Function
from marimo._utils.parse_dataclass import parse_raw


@dataclass
class GetDataFrameResponse:
    url: str
    has_more: bool
    total_rows: int
    # List of column names that are actually row headers
    # This really only applies to Pandas, that has special index columns
    row_headers: List[str]
    supports_code_sample: bool
    field_types: FieldTypes


@dataclass
class GetColumnValuesArgs:
    column: str


@dataclass
class GetColumnValuesResponse:
    values: List[str | int | float]
    too_many_values: bool


class ColumnNotFound(Exception):
    def __init__(self, column: str):
        self.column = column
        super().__init__(f"Column {column} does not exist")


class GetDataFrameError(Exception):
    def __init__(self, error: str):
        self.error = error
        super().__init__(error)


@mddoc
class dataframe(UIElement[Dict[str, Any], DataFrameType]):
    """
    Run transformations on a DataFrame or series.
    Currently only Pandas or Polars DataFrames are supported.

    **Example.**

    ```python
    dataframe = mo.ui.dataframe(data)
    ```

    **Attributes.**

    - `value`: the transformed DataFrame or series

    **Initialization Args.**

    - `df`: the DataFrame or series to transform
    - `page_size`: the number of rows to show in the table
    """

    _name: Final[str] = "marimo-dataframe"

    # Only get the first 100 (for performance reasons)
    # Could make this configurable in the arguments later if desired.
    DISPLAY_LIMIT = 100

    def __init__(
        self,
        df: DataFrameType,
        on_change: Optional[Callable[[DataFrameType], None]] = None,
        page_size: Optional[int] = 5,
    ) -> None:
        # This will raise an error if the dataframe type is not supported.
        handler = get_handler_for_dataframe(df)

        # HACK: this is a hack to get the name of the variable that was passed
        dataframe_name = "df"
        try:
            frame = inspect.currentframe()
            if frame is not None and frame.f_back is not None:
                for (
                    var_name,
                    var_value,
                ) in frame.f_back.f_locals.items():
                    if var_value is df:
                        dataframe_name = var_name
                        break
        except Exception:
            pass

        self._data = df
        self._handler = handler
        self._manager = get_table_manager(df)
        self._transform_container = TransformsContainer[DataFrameType](
            df, handler
        )
        self._error: Optional[str] = None

        super().__init__(
            component_name=dataframe._name,
            initial_value={
                "transforms": [],
            },
            on_change=on_change,
            label="",
            args={
                "columns": self._get_column_types(),
                "dataframe-name": dataframe_name,
                "total": self._manager.get_num_rows(),
                "page-size": page_size,
            },
            functions=(
                Function(
                    name=self.get_dataframe.__name__,
                    arg_cls=EmptyArgs,
                    function=self.get_dataframe,
                ),
                Function(
                    name=self.get_column_values.__name__,
                    arg_cls=GetColumnValuesArgs,
                    function=self.get_column_values,
                ),
                Function(
                    name=self.search.__name__,
                    arg_cls=SearchTableArgs,
                    function=self.search,
                ),
            ),
        )

    def _get_column_types(self) -> List[List[Union[str, int]]]:
        return [
            [name, dtype[0], dtype[1]]
            for name, dtype in self._manager.get_field_types().items()
        ]

    def get_dataframe(self, _args: EmptyArgs) -> GetDataFrameResponse:
        if self._error is not None:
            raise GetDataFrameError(self._error)

        manager = get_table_manager(self._data)
        response = self.search(SearchTableArgs(page_size=10, page_number=0))
        return GetDataFrameResponse(
            url=str(response.data),
            total_rows=response.total_rows,
            has_more=False,
            row_headers=manager.get_row_headers(),
            supports_code_sample=self._handler.supports_code_sample(),
            field_types=manager.get_field_types(),
        )

    def get_column_values(
        self, args: GetColumnValuesArgs
    ) -> GetColumnValuesResponse:
        """Get all the unique values in a column."""
        LIMIT = 500

        columns = self._manager.get_column_names()
        if args.column not in columns:
            raise ColumnNotFound(args.column)

        # We get the unique values from the original dataframe, not the
        # transformed one
        unique_values = self._manager.get_unique_column_values(args.column)
        if len(unique_values) <= LIMIT:
            return GetColumnValuesResponse(
                values=list(sorted(unique_values, key=str)),
                too_many_values=False,
            )
        else:
            return GetColumnValuesResponse(
                values=[],
                too_many_values=True,
            )

    def _convert_value(self, value: Dict[str, Any]) -> DataFrameType:
        if value is None:
            self._error = None
            return self._data

        try:
            transformations = parse_raw(value, Transformations)
            result = self._transform_container.apply(transformations)
            self._error = None
            return result
        except Exception as e:
            error = "Error applying dataframe transform: %s\n\n" % str(e)
            sys.stderr.write(error)
            self._error = error
            return self._data

    def search(self, args: SearchTableArgs) -> SearchTableResponse:
        offset = args.page_number * args.page_size

        # If no query or sort, return nothing
        # The frontend will just show the original data
        if not args.query and not args.sort and not args.filters:
            manager = get_table_manager(self._value)
            data = manager.take(args.page_size, offset).to_data()
            return SearchTableResponse(
                data=data,
                total_rows=manager.get_num_rows(force=True) or 0,
            )

        # Apply filters, query, and functools.sort using the cached method
        result = self._apply_filters_query_sort(
            args.query,
            args.sort,
        )

        # Save the manager to be used for selection
        data = result.take(args.page_size, offset).to_data()
        return SearchTableResponse(
            data=data,
            total_rows=result.get_num_rows(force=True) or 0,
        )

    def _apply_filters_query_sort(
        self,
        query: Optional[str],
        sort: Optional[SortArgs],
    ) -> TableManager[Any]:
        result = get_table_manager(self._value)

        if query:
            result = result.search(query)

        if sort:
            result = result.sort_values(sort.by, sort.descending)

        return result
