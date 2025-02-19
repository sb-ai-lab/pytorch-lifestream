import logging
from typing import List, Dict, Union

import numpy as np
import pandas as pd
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

from ptls.preprocessing.base.data_preprocessor import DataPreprocessor
from ptls.preprocessing.base.transformation.col_category_transformer import ColCategoryTransformer
from ptls.preprocessing.base.transformation.col_numerical_transformer import ColTransformer
from ptls.preprocessing.pyspark.category_identity_encoder import CategoryIdentityEncoder
from ptls.preprocessing.pyspark.col_identity_transformer import ColIdentityEncoder
from ptls.preprocessing.pyspark.event_time import DatetimeToTimestamp
from ptls.preprocessing.pyspark.frequency_encoder import FrequencyEncoder
from ptls.preprocessing.pyspark.user_group_transformer import UserGroupTransformer

logger = logging.getLogger(__name__)


class PysparkDataPreprocessor(DataPreprocessor):
    """Data preprocessor based on pyspark.sql.DataFrame

    During preprocess it
        * transforms `cols_event_time` column with date and time
        * encodes category columns `cols_category` into ints;
        * apply logarithm transformation to `cols_log_norm' columns;
        * (Optional) select the last `max_trx_count` transactions for each `col_id`;
        * groups flat data by `col_id`;
        * arranges data into list of dicts with features

    Args:
        col_id:  name of column with ids
        col_event_time: name of column with time and date
        cols_category: list of category columns
        cols_identity: list of columns to be passed as is without any transformation
        max_trx_count: Default: 5000. Used when `remove_long_trx`=True
        max_cat_num: Maximum category number

    """

    def __init__(
        self,
        col_id: str,
        col_event_time: Union[str, ColTransformer],
        event_time_transformation: str = "dt_to_timestamp",
        cols_category: List[Union[str, ColCategoryTransformer]] = None,
        category_transformation: str = "frequency",
        cols_numerical: List[str] = None,
        cols_identity: List[str] = None,
        cols_last_item: List[str] = None,
        max_trx_count: int = None,
        max_cat_num: Union[Dict[str, int], int] = 10000,
    ):

        if cols_category is None:
            cols_category = []
        if cols_numerical is None:
            cols_numerical = []
        if cols_identity is None:
            cols_identity = []
        if cols_last_item is None:
            cols_last_item = []

        if not isinstance(col_event_time, str):
            ct_event_time = col_event_time  # use as is
        elif event_time_transformation == "dt_to_timestamp":
            ct_event_time = DatetimeToTimestamp(col_name_original=col_event_time)
        elif event_time_transformation == "none":
            ct_event_time = ColIdentityEncoder(
                col_name_original=col_event_time,
                col_name_target="event_time",
                is_drop_original_col=False,
            )
        else:
            raise AttributeError(
                f"incorrect event_time parameters combination: "
                f'`ct_event_time` = "{col_event_time}" '
                f'`event_time_transformation` = "{event_time_transformation}"'
            )

        cts_category = []
        for col in cols_category:
            if not isinstance(col, str):
                cts_category.append(col)  # use as is
            elif category_transformation == "frequency":
                if isinstance(max_cat_num, dict):
                    mc = max_cat_num.get(col)
                else:
                    mc = max_cat_num
                cts_category.append(
                    FrequencyEncoder(col_name_original=col, max_cat_num=mc)
                )
            elif category_transformation == "none":
                cts_category.append(CategoryIdentityEncoder(col_name_original=col))
            else:
                raise AttributeError(
                    f"incorrect category parameters combination: "
                    f'`cols_category[i]` = "{col}" '
                    f'`category_transformation` = "{category_transformation}"'
                )

        cts_numerical = [
            ColIdentityEncoder(col_name_original=col) for col in cols_numerical
        ]
        t_user_group = UserGroupTransformer(
            col_name_original=col_id,
            cols_last_item=cols_last_item,
            max_trx_count=max_trx_count,
        )

        super().__init__(
            col_id=col_id,
            col_event_time=ct_event_time,
            cols_category=cts_category,
            cols_numerical=cts_numerical,
            cols_identity=cols_identity,
            t_user_group=t_user_group,
        )

    @staticmethod
    def _td_default(df, cols_event_time):
        w = Window().orderBy(cols_event_time)
        tmp_df = df.select(cols_event_time).distinct()
        tmp_df = tmp_df.withColumn("event_time", F.row_number().over(w) - 1)
        df = df.join(tmp_df, on=cols_event_time)
        return df

    @staticmethod
    def _td_float(df, col_event_time):
        logger.info("To-float time transformation begins...")
        df = df.withColumn("event_time", F.col(col_event_time).astype("float"))
        logger.info("To-float time transformation ends")
        return df

    @staticmethod
    def _td_gender(df: pd.DataFrame, col_event_time: str) -> pd.DataFrame:
        """Gender-dataset-like transformation
        'd hh:mm:ss' -> float where integer part is day number and fractional part is seconds from day begin
        '1 00:00:00' -> 1.0
        '1 12:00:00' -> 1.5
        '1 01:00:00' -> 1 + 1 / 24
        '2 23:59:59' -> 1.99
        '432 12:00:00' -> 432.5   '000432 12:00:00'

        Args:
            df: DataFrame
            col_event_time: name of column with datetime

        """
        logger.info("Gender-dataset-like time transformation begins...")
        df = df.withColumn(
            "_et_day",
            F.substring(F.lpad(F.col(col_event_time), 15, "0"), 1, 6).cast("float"),
        )

        df = df.withColumn(
            "_et_time", F.substring(F.lpad(F.col(col_event_time), 15, "0"), 8, 8)
        )
        df = df.withColumn("_et_time", F.regexp_replace("_et_time", r"\:60$", ":59"))
        df = df.withColumn(
            "_et_time", F.unix_timestamp("_et_time", "HH:mm:ss") / (24 * 60 * 60)
        )

        df = df.withColumn("event_time", F.col("_et_day") + F.col("_et_time"))
        df = df.drop("_et_day", "_et_time")
        logger.info("Gender-dataset-like time transformation ends")
        return df

    def _td_hours(self, df: pd.DataFrame, col_event_time: str) -> pd.DataFrame:
        """Transform time to hours

        Args:
            df: DataFrame
            col_event_time: name of column with datetime

        """
        logger.info("To hours time transformation begins...")
        df = df.withColumn(
            "_dt", (F.col(col_event_time)).cast(dataType=T.TimestampType())
        )
        df = df.withColumn(
            "event_time", ((F.col("_dt")).cast("float") - self.time_min) / 3600
        )
        df = df.drop("_dt")
        logger.info("To hours time transformation ends")
        return df

    def _reset(self):
        """Reset internal data-dependent state of the preprocessor, if necessary.
        __init__ parameters are not touched.

        """
        self.time_min = None
        self.remove_long_trx = False
        self.max_trx_count = 5000
        # super()._reset()

    def pd_hist(self, df, name, bins=10):
        # logger.info('pd_hist begin')
        # logger.info(f'sf = {self.config.sample_fraction}')
        data = df.select(name)
        if self.config.sample_fraction is not None:
            data = data.sample(fraction=self.config.sample_fraction)
        data = data.toPandas()[name]

        if data.dtype.kind == "f":
            round_len = 1 if data.max() > bins + 1 else 2
            bins = np.linspace(data.min(), data.max(), bins + 1).round(round_len)
        elif np.percentile(data, 99) - data.min() > bins - 1:
            bins = np.linspace(data.min(), np.percentile(data, 99), bins).astype(
                int
            ).tolist() + [int(data.max() + 1)]
        else:
            bins = np.arange(data.min(), data.max() + 2, 1).astype(int)
        df = pd.cut(data, bins, right=False).rename(name)
        df = df.to_frame().assign(cnt=1).groupby(name)[["cnt"]].sum()
        df["% of total"] = df["cnt"] / df["cnt"].sum()
        return df

    def fit_transform(self, X):
        for ct in self._all_col_transformers:
            X = ct.fit_transform(X)
        return X

    def transform(self, x):
        for ct in self._all_col_transformers:
            x = ct.transform(x)
        return x
