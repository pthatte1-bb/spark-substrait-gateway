# SPDX-License-Identifier: Apache-2.0
"""Tests for the Spark to Substrait Gateway server."""
import pyarrow as pa
import pyarrow.parquet as pq
import pyspark
import pytest
from gateway.tests.conftest import find_tpch
from gateway.tests.plan_validator import utilizes_valid_plans
from hamcrest import assert_that, equal_to
from pyspark import Row
from pyspark.errors.exceptions.connect import SparkConnectGrpcException
from pyspark.sql.functions import (
    bit_length,
    broadcast,
    btrim,
    char_length,
    character_length,
    coalesce,
    col,
    concat,
    concat_ws,
    contains,
    endswith,
    equal_null,
    expr,
    greatest,
    ifnull,
    instr,
    isnan,
    isnotnull,
    isnull,
    lcase,
    least,
    left,
    length,
    lit,
    locate,
    lower,
    lpad,
    ltrim,
    named_struct,
    nanvl,
    nullif,
    nvl,
    nvl2,
    octet_length,
    position,
    regexp,
    regexp_like,
    repeat,
    replace,
    right,
    rlike,
    rpad,
    rtrim,
    startswith,
    substr,
    substring,
    trim,
    ucase,
    upper,
)
from pyspark.testing import assertDataFrameEqual


def create_parquet_table(spark_session, table_name: str, table: pa.Table):
    """Creates a parquet table from a PyArrow table and registers it to the session."""
    pq.write_table(table, f'{table_name}.parquet')
    table_df = spark_session.read.parquet(f'{table_name}.parquet')
    table_df.createOrReplaceTempView(table_name)
    return spark_session.table(table_name)


@pytest.fixture(autouse=True)
def mark_dataframe_tests_as_xfail(request):
    """Marks a subset of tests as expected to be fail."""
    source = request.getfixturevalue('source')
    originalname = request.keywords.node.originalname
    if source == 'gateway-over-datafusion':
        pytest.importorskip("datafusion.substrait")
        if originalname in ['test_column_getfield', 'test_column_getitem']:
            request.node.add_marker(pytest.mark.xfail(reason='structs not handled'))
    elif originalname == 'test_column_getitem':
        request.node.add_marker(pytest.mark.xfail(reason='maps and lists not handled'))
    elif source == 'spark' and originalname == 'test_subquery_alias':
        pytest.xfail('Spark supports subquery_alias but everyone else does not')

    if source != 'spark' and originalname.startswith('test_unionbyname'):
        request.node.add_marker(pytest.mark.xfail(reason='unionByName not supported in Substrait'))
    if source != 'spark' and originalname.startswith('test_exceptall'):
        request.node.add_marker(pytest.mark.xfail(reason='exceptAll not supported in Substrait'))
    if source == 'gateway-over-duckdb' and originalname in ['test_union', 'test_unionall']:
        request.node.add_marker(pytest.mark.xfail(reason='DuckDB treats all unions as distinct'))
    if source == 'gateway-over-datafusion' and originalname == 'test_subtract':
        request.node.add_marker(pytest.mark.xfail(reason='subtract not supported'))
    if source == 'gateway-over-datafusion' and originalname == 'test_intersect':
        request.node.add_marker(pytest.mark.xfail(reason='intersect not supported'))
    if source == 'gateway-over-datafusion' and originalname == 'test_offset':
        request.node.add_marker(pytest.mark.xfail(reason='offset not supported'))
    if source == 'gateway-over-datafusion' and originalname == 'test_broadcast':
        request.node.add_marker(pytest.mark.xfail(reason='duplicate name problem with joins'))
    if source == 'gateway-over-duckdb' and originalname == 'test_coalesce':
        request.node.add_marker(pytest.mark.xfail(reason='missing Substrait mapping'))
    if source == 'spark' and originalname == 'test_isnan':
        request.node.add_marker(pytest.mark.xfail(reason='None not preserved'))
    if source == 'gateway-over-datafusion' and originalname in [
        'test_isnan', 'test_nanvl', 'test_least', 'test_greatest']:
        request.node.add_marker(pytest.mark.xfail(reason='missing Substrait mapping'))
    if source != 'spark' and originalname == 'test_expr':
        request.node.add_marker(pytest.mark.xfail(reason='SQL support needed in gateway'))
    if source != 'spark' and originalname == 'test_named_struct':
        request.node.add_marker(pytest.mark.xfail(reason='needs better type tracking in gateway'))
    if source == 'spark' and originalname == 'test_nullif':
        request.node.add_marker(pytest.mark.xfail(reason='internal Spark type error'))
    if source == 'gateway-over-duckdb' and originalname == 'test_nullif':
        request.node.add_marker(pytest.mark.xfail(reason='argument count issue in DuckDB mapping'))
    if source == 'gateway-over-datafusion' and originalname == 'test_contains':
        request.node.add_marker(pytest.mark.xfail(reason='contains returns position not binary'))
    if source != 'spark' and originalname in ['test_locate', 'test_position']:
        request.node.add_marker(pytest.mark.xfail(reason='no direct Substrait analog'))
    if source == 'gateway-over-duckdb' and originalname == 'test_octet_length':
        request.node.add_marker(pytest.mark.xfail(reason='varchar octet_length not supported'))


# ruff: noqa: E712
class TestDataFrameAPI:
    """Tests of the dataframe side of SparkConnect."""

    def test_collect(self, users_dataframe):
        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.collect()

        assert len(outcome) == 100

    # pylint: disable=singleton-comparison
    def test_filter(self, users_dataframe):
        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.filter(col('paid_for_service') == True).collect()

        assert len(outcome) == 29

    def test_dropna(self, spark_session, caplog):
        schema = pa.schema({'name': pa.string(), 'age': pa.int32()})
        table = pa.Table.from_pydict(
            {'name': [None, 'Joe', 'Sarah', None],
             'age': [99, None, 42, None]}, schema=schema)
        test_df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(test_df, caplog):
            outcome = test_df.dropna().collect()

        assert len(outcome) == 1

    def test_dropna_by_name(self, spark_session, caplog):
        schema = pa.schema({'name': pa.string(), 'age': pa.int32()})
        table = pa.Table.from_pydict(
            {'name': [None, 'Joe', 'Sarah', None],
             'age': [99, None, 42, None]}, schema=schema)
        test_df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(test_df, caplog):
            outcome = test_df.dropna(subset='name').collect()

        assert len(outcome) == 2

    def test_dropna_by_count(self, spark_session, caplog):
        schema = pa.schema({'name': pa.string(), 'age': pa.int32()})
        table = pa.Table.from_pydict(
            {'name': [None, 'Joe', 'Sarah', None],
             'age': [99, None, 42, None]}, schema=schema)
        test_df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(test_df, caplog):
            outcome = test_df.dropna(thresh=1).collect()

        assert len(outcome) == 3

    # pylint: disable=singleton-comparison
    def test_filter_with_show(self, users_dataframe, capsys):
        expected = '''+-------------+---------------+----------------+
|      user_id|           name|paid_for_service|
+-------------+---------------+----------------+
|user669344115|   Joshua Brown|            true|
|user282427709|Michele Carroll|            true|
+-------------+---------------+----------------+

'''
        with utilizes_valid_plans(users_dataframe):
            users_dataframe.filter(col('paid_for_service') == True).limit(2).show()
            outcome = capsys.readouterr().out

        assert_that(outcome, equal_to(expected))

    # pylint: disable=singleton-comparison
    def test_filter_with_show_with_limit(self, users_dataframe, capsys):
        expected = '''+-------------+------------+----------------+
|      user_id|        name|paid_for_service|
+-------------+------------+----------------+
|user669344115|Joshua Brown|            true|
+-------------+------------+----------------+
only showing top 1 row

'''
        with utilizes_valid_plans(users_dataframe):
            users_dataframe.filter(col('paid_for_service') == True).show(1)
            outcome = capsys.readouterr().out

        assert_that(outcome, equal_to(expected))

    # pylint: disable=singleton-comparison
    def test_filter_with_show_and_truncate(self, users_dataframe, capsys):
        expected = '''+----------+----------+----------------+
|   user_id|      name|paid_for_service|
+----------+----------+----------------+
|user669...|Joshua ...|            true|
+----------+----------+----------------+

'''
        users_dataframe.filter(col('paid_for_service') == True).limit(1).show(truncate=10)
        outcome = capsys.readouterr().out
        assert_that(outcome, equal_to(expected))

    def test_count(self, users_dataframe):
        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.count()

        assert outcome == 100

    def test_limit(self, users_dataframe):
        expected = [
            Row(user_id='user849118289', name='Brooke Jones', paid_for_service=False),
            Row(user_id='user954079192', name='Collin Frank', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.limit(2).collect()

        assertDataFrameEqual(outcome, expected)

    def test_with_column(self, users_dataframe):
        expected = [
            Row(user_id='user849118289', name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumn('user_id', col('user_id')).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_with_column_changed(self, users_dataframe):
        expected = [
            Row(user_id='user849118289', name='Brooke', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumn(
                'name',
                substring(col('name'), 1, 6)).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_with_column_added(self, users_dataframe):
        expected = [
            Row(user_id='user849118289', name='Brooke Jones', paid_for_service=False,
                not_paid_for_service=True),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumn(
                'not_paid_for_service',
                ~users_dataframe.paid_for_service).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_with_column_renamed(self, users_dataframe):
        expected = [
            Row(old_user_id='user849118289', name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumnRenamed('user_id', 'old_user_id').limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_with_columns(self, users_dataframe):
        expected = [
            Row(user_id='user849118289', name='Brooke', paid_for_service=False,
                not_paid_for_service=True),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumns(
                {'user_id': col('user_id'),
                 'name': substring(col('name'), 1, 6),
                 'not_paid_for_service': ~users_dataframe.paid_for_service,
                 }).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_with_columns_renamed(self, users_dataframe):
        expected = [
            Row(old_user_id='user849118289', old_name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumnsRenamed({'user_id': 'old_user_id',
                                                          'name': 'old_name'}).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_drop(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.drop(users_dataframe.user_id).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_drop_by_name(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.drop('user_id').limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == list(expected[0].asDict().keys())

    def test_drop_all(self, users_dataframe, source):

        if source == 'spark':
            outcome = users_dataframe.drop('user_id').drop('name').drop('paid_for_service').limit(
                1).collect()

            assert not outcome[0].asDict().keys()
        else:
            with pytest.raises(SparkConnectGrpcException):
                users_dataframe.drop('user_id').drop('name').drop('paid_for_service').limit(
                    1).collect()

    def test_alias(self, users_dataframe):
        expected = [
            Row(foo='user849118289'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(col('user_id').alias('foo')).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == ['foo']

    def test_subquery_alias(self, users_dataframe):
        with pytest.raises(Exception) as exc_info:
            users_dataframe.select(col('user_id')).alias('foo').limit(1).collect()

        assert exc_info.match('Subquery alias relations are not yet implemented')

    def test_name(self, users_dataframe):
        expected = [
            Row(foo='user849118289'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(col('user_id').name('foo')).limit(1).collect()

        assertDataFrameEqual(outcome, expected)
        assert list(outcome[0].asDict().keys()) == ['foo']

    def test_cast(self, users_dataframe):
        expected = [
            Row(user_id=849, name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumn(
                'user_id',
                substring(col('user_id'), 5, 3).cast('integer')).limit(1).collect()

        assertDataFrameEqual(outcome, expected)

    def test_getattr(self, users_dataframe):
        expected = [
            Row(user_id='user669344115'),
            Row(user_id='user849118289'),
            Row(user_id='user954079192'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(users_dataframe.user_id).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_getitem(self, users_dataframe):
        expected = [
            Row(user_id='user669344115'),
            Row(user_id='user849118289'),
            Row(user_id='user954079192'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(users_dataframe['user_id']).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_column_getfield(self, spark_session, caplog):
        expected = [
            Row(answer='b', answer2=1),
        ]

        struct_type = pa.struct([('a', pa.int64()), ('b', pa.string())])
        data = [
            {'a': 1, 'b': 'b'}
        ]
        struct_array = pa.array(data, type=struct_type)
        table = pa.Table.from_arrays([struct_array], names=['r'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(df.r.getField("b"), df.r.a).collect()

        assertDataFrameEqual(outcome, expected)

    def test_column_getitem(self, spark_session):
        expected = [
            Row(answer=1, answer2='value'),
        ]

        list_array = pa.array([[1, 2]], type=pa.list_(pa.int64()))
        map_array = pa.array([{"key": "value"}],
                             type=pa.map_(pa.string(), pa.string(), False))
        table = pa.Table.from_arrays([list_array, map_array], names=['l', 'd'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(df.l.getItem(0), df.d.getItem("key")).collect()

        assertDataFrameEqual(outcome, expected)

    def test_astype(self, users_dataframe):
        expected = [
            Row(user_id=849, name='Brooke Jones', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.withColumn(
                'user_id',
                substring(col('user_id'), 5, 3).astype('integer')).limit(1).collect()

        assertDataFrameEqual(outcome, expected)

    def test_join(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=5, n_name='ETHIOPIA', n_regionkey=0,
                n_comment='ven packages wake quickly. regu', s_suppkey=2,
                s_name='Supplier#000000002', s_address='89eJ5ksX3ImxJQBvxObC,', s_nationkey=5,
                s_phone='15-679-861-2259', s_acctbal=4032.68,
                s_comment=' slyly bold instructions. idle dependen'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')
            supplier = spark_session_with_tpch_dataset.table('supplier')

            nat = nation.join(supplier, col('n_nationkey') == col('s_nationkey'))
            outcome = nat.filter(col('s_suppkey') == 2).limit(1).collect()

        assertDataFrameEqual(outcome, expected)

    def test_crossjoin(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=1, n_name='ARGENTINA', s_name='Supplier#000000002'),
            Row(n_nationkey=2, n_name='BRAZIL', s_name='Supplier#000000002'),
            Row(n_nationkey=3, n_name='CANADA', s_name='Supplier#000000002'),
            Row(n_nationkey=4, n_name='EGYPT', s_name='Supplier#000000002'),
            Row(n_nationkey=5, n_name='ETHIOPIA', s_name='Supplier#000000002'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')
            supplier = spark_session_with_tpch_dataset.table('supplier')

            nat = nation.crossJoin(supplier).filter(col('s_suppkey') == 2)
            outcome = nat.select('n_nationkey', 'n_name', 's_name').limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_union(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')

            outcome = nation.union(nation).filter(col('n_nationkey') == 23).collect()

        assertDataFrameEqual(outcome, expected)

    def test_union_distinct(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')

            outcome = nation.union(nation).distinct().filter(col('n_nationkey') == 23).collect()

        assertDataFrameEqual(outcome, expected)

    def test_unionall(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')

            outcome = nation.unionAll(nation).filter(col('n_nationkey') == 23).collect()

        assertDataFrameEqual(outcome, expected)

    def test_exceptall(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=21, n_name='VIETNAM', n_regionkey=2,
                n_comment='hely enticingly express accounts. even, final '),
            Row(n_nationkey=21, n_name='VIETNAM', n_regionkey=2,
                n_comment='hely enticingly express accounts. even, final '),
            Row(n_nationkey=22, n_name='RUSSIA', n_regionkey=3,
                n_comment=' requests against the platelets use never according to the '
                          'quickly regular pint'),
            Row(n_nationkey=22, n_name='RUSSIA', n_regionkey=3,
                n_comment=' requests against the platelets use never according to the '
                          'quickly regular pint'),
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
            Row(n_nationkey=24, n_name='UNITED STATES', n_regionkey=1,
                n_comment='y final packages. slow foxes cajole quickly. quickly silent platelets '
                          'breach ironic accounts. unusual pinto be'),
            Row(n_nationkey=24, n_name='UNITED STATES', n_regionkey=1,
                n_comment='y final packages. slow foxes cajole quickly. quickly silent platelets '
                          'breach ironic accounts. unusual pinto be'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation').filter(col('n_nationkey') > 20)
            nation1 = nation.union(nation)
            nation2 = nation.filter(col('n_nationkey') == 23)

            outcome = nation1.exceptAll(nation2).collect()

        assertDataFrameEqual(outcome, expected)

    def test_distinct(self, spark_session):
        expected = [
            Row(a=1, b=10, c='a'),
            Row(a=2, b=11, c='a'),
            Row(a=3, b=12, c='a'),
            Row(a=4, b=13, c='a'),
        ]

        int1_array = pa.array([1, 2, 3, 3, 4], type=pa.int32())
        int2_array = pa.array([10, 11, 12, 12, 13], type=pa.int32())
        string_array = pa.array(['a', 'a', 'a', 'a', 'a'], type=pa.string())
        table = pa.Table.from_arrays([int1_array, int2_array, string_array],
                                     names=['a', 'b', 'c'])

        df = create_parquet_table(spark_session, 'mytesttable1', table)

        with utilizes_valid_plans(df):
            outcome = df.distinct().collect()

        assertDataFrameEqual(outcome, expected)

    def test_dropduplicates(self, spark_session):
        expected = [
            Row(a=1, b=10, c='a'),
            Row(a=2, b=11, c='a'),
            Row(a=3, b=12, c='a'),
            Row(a=4, b=13, c='a'),
        ]

        int1_array = pa.array([1, 2, 3, 3, 4], type=pa.int32())
        int2_array = pa.array([10, 11, 12, 12, 13], type=pa.int32())
        string_array = pa.array(['a', 'a', 'a', 'a', 'a'], type=pa.string())
        table = pa.Table.from_arrays([int1_array, int2_array, string_array],
                                     names=['a', 'b', 'c'])

        df = create_parquet_table(spark_session, 'mytesttable1', table)

        with utilizes_valid_plans(df):
            outcome = df.dropDuplicates().collect()

        assertDataFrameEqual(outcome, expected)

    def test_colregex(self, spark_session, caplog):
        expected = [
            Row(a1=1, col2='a'),
            Row(a1=2, col2='b'),
            Row(a1=3, col2='c'),
        ]

        int_array = pa.array([1, 2, 3], type=pa.int32())
        string_array = pa.array(['a', 'b', 'c'], type=pa.string())
        table = pa.Table.from_arrays([int_array, int_array, string_array, string_array],
                                     names=['a1', 'c', 'col', 'col2'])

        df = create_parquet_table(spark_session, 'mytesttable1', table)

        with utilizes_valid_plans(df, caplog):
            outcome = df.select(df.colRegex("`(c.l|a)?[0-9]`")).collect()

        assertDataFrameEqual(outcome, expected)

    def test_subtract(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=21, n_name='VIETNAM', n_regionkey=2,
                n_comment='hely enticingly express accounts. even, final '),
            Row(n_nationkey=22, n_name='RUSSIA', n_regionkey=3,
                n_comment=' requests against the platelets use never according to the '
                          'quickly regular pint'),
            Row(n_nationkey=24, n_name='UNITED STATES', n_regionkey=1,
                n_comment='y final packages. slow foxes cajole quickly. quickly silent platelets '
                          'breach ironic accounts. unusual pinto be'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation').filter(col('n_nationkey') > 20)
            nation1 = nation.union(nation)
            nation2 = nation.filter(col('n_nationkey') == 23)

            outcome = nation1.subtract(nation2).collect()

        assertDataFrameEqual(outcome, expected)

    def test_intersect(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_nationkey=23, n_name='UNITED KINGDOM', n_regionkey=3,
                n_comment='eans boost carefully special requests. accounts are. carefull'),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')
            nation1 = nation.union(nation).filter(col('n_nationkey') >= 23)
            nation2 = nation.filter(col('n_nationkey') <= 23)

            outcome = nation1.intersect(nation2).collect()

        assertDataFrameEqual(outcome, expected)

    def test_unionbyname(self, spark_session):
        expected = [
            Row(a=1, b=2, c=3),
            Row(a=4, b=5, c=6),
        ]

        int1_array = pa.array([1], type=pa.int32())
        int2_array = pa.array([2], type=pa.int32())
        int3_array = pa.array([3], type=pa.int32())
        table = pa.Table.from_arrays([int1_array, int2_array, int3_array], names=['a', 'b', 'c'])
        int4_array = pa.array([4], type=pa.int32())
        int5_array = pa.array([5], type=pa.int32())
        int6_array = pa.array([6], type=pa.int32())
        table2 = pa.Table.from_arrays([int4_array, int5_array, int6_array], names=['a', 'b', 'c'])

        df = create_parquet_table(spark_session, 'mytesttable1', table)
        df2 = create_parquet_table(spark_session, 'mytesttable2', table2)

        with utilizes_valid_plans(df):
            outcome = df.unionByName(df2).collect()
            assertDataFrameEqual(outcome, expected)

    def test_unionbyname_with_mismatched_columns(self, spark_session):
        int1_array = pa.array([1], type=pa.int32())
        int2_array = pa.array([2], type=pa.int32())
        int3_array = pa.array([3], type=pa.int32())
        table = pa.Table.from_arrays([int1_array, int2_array, int3_array], names=['a', 'b', 'c'])
        int4_array = pa.array([4], type=pa.int32())
        int5_array = pa.array([5], type=pa.int32())
        int6_array = pa.array([6], type=pa.int32())
        table2 = pa.Table.from_arrays([int4_array, int5_array, int6_array], names=['b', 'c', 'd'])

        df = create_parquet_table(spark_session, 'mytesttable1', table)
        df2 = create_parquet_table(spark_session, 'mytesttable2', table2)

        with pytest.raises(pyspark.errors.exceptions.captured.AnalysisException):
            df.unionByName(df2).collect()

    def test_unionbyname_with_missing_columns(self, spark_session):
        expected = [
            Row(a=1, b=2, c=3, d=None),
            Row(a=None, b=4, c=5, d=6),
        ]

        int1_array = pa.array([1], type=pa.int32())
        int2_array = pa.array([2], type=pa.int32())
        int3_array = pa.array([3], type=pa.int32())
        table = pa.Table.from_arrays([int1_array, int2_array, int3_array], names=['a', 'b', 'c'])
        int4_array = pa.array([4], type=pa.int32())
        int5_array = pa.array([5], type=pa.int32())
        int6_array = pa.array([6], type=pa.int32())
        table2 = pa.Table.from_arrays([int4_array, int5_array, int6_array], names=['b', 'c', 'd'])

        df = create_parquet_table(spark_session, 'mytesttable1', table)
        df2 = create_parquet_table(spark_session, 'mytesttable2', table2)

        with utilizes_valid_plans(df):
            outcome = df.unionByName(df2, allowMissingColumns=True).collect()
            assertDataFrameEqual(outcome, expected)

    def test_between(self, spark_session_with_tpch_dataset):
        expected = [
            Row(n_name='ARGENTINA', is_between=False),
            Row(n_name='BRAZIL', is_between=False),
            Row(n_name='CANADA', is_between=False),
            Row(n_name='EGYPT', is_between=True),
            Row(n_name='ETHIOPIA', is_between=False),
        ]

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            nation = spark_session_with_tpch_dataset.table('nation')

            outcome = nation.select(
                nation.n_name,
                nation.n_regionkey.between(2, 4).name('is_between')).limit(5).collect()

            assertDataFrameEqual(outcome, expected)

    def test_eqnullsafe(self, spark_session):
        expected = [
            Row(a=None, b=False, c=True),
            Row(a=True, b=True, c=False),
        ]
        expected2 = [
            Row(a=False, b=False, c=True),
            Row(a=False, b=True, c=False),
            Row(a=True, b=False, c=False),
        ]

        string_array = pa.array(['foo', None, None], type=pa.string())
        float_array = pa.array([float('NaN'), 42.0, None], type=pa.float64())
        table = pa.Table.from_arrays([string_array, float_array], names=['s', 'f'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(df.s == 'foo',
                                df.s.eqNullSafe('foo'),
                                df.s.eqNullSafe(None)).limit(2).collect()
            assertDataFrameEqual(outcome, expected)

            outcome = df.select(df.f.eqNullSafe(None),
                                df.f.eqNullSafe(float('NaN')),
                                df.f.eqNullSafe(42.0)).collect()
            assertDataFrameEqual(outcome, expected2)

    def test_bitwise(self, spark_session):
        expected = [
            Row(a=0, b=42, c=42),
            Row(a=42, b=42, c=0),
            Row(a=8, b=255, c=247),
            Row(a=None, b=None, c=None),
        ]

        int_array = pa.array([221, 0, 42, None], type=pa.int64())
        table = pa.Table.from_arrays([int_array], names=['i'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(df.i.bitwiseAND(42),
                                df.i.bitwiseOR(42),
                                df.i.bitwiseXOR(42)).collect()
            assertDataFrameEqual(outcome, expected)

    def test_first(self, users_dataframe):
        expected = Row(user_id='user012015386', name='Kelly Mcdonald', paid_for_service=False)

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.sort('user_id').first()

        assert outcome == expected

    def test_head(self, users_dataframe):
        expected = [
            Row(user_id='user012015386', name='Kelly Mcdonald', paid_for_service=False),
            Row(user_id='user041132632', name='Tina Atkinson', paid_for_service=False),
            Row(user_id='user056872864', name='Kenneth Castro', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.sort('user_id').head(3)
            assertDataFrameEqual(outcome, expected)

    def test_take(self, users_dataframe):
        expected = [
            Row(user_id='user012015386', name='Kelly Mcdonald', paid_for_service=False),
            Row(user_id='user041132632', name='Tina Atkinson', paid_for_service=False),
            Row(user_id='user056872864', name='Kenneth Castro', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.sort('user_id').take(3)
            assertDataFrameEqual(outcome, expected)

    def test_offset(self, users_dataframe):
        expected = [
            Row(user_id='user056872864', name='Kenneth Castro', paid_for_service=False),
            Row(user_id='user058232666', name='Collin Goodwin', paid_for_service=False),
            Row(user_id='user065694278', name='Rachel Mclean', paid_for_service=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.sort('user_id').offset(2).head(3)
            assertDataFrameEqual(outcome, expected)

    def test_tail(self, users_dataframe, source):
        expected = [
            Row(user_id='user990459354', name='Kevin Hall', paid_for_service=False),
            Row(user_id='user995187670', name='Rebecca Valentine', paid_for_service=False),
            Row(user_id='user995208610', name='Helen Clark', paid_for_service=False),
        ]

        if source == 'spark':
            outcome = users_dataframe.sort('user_id').tail(3)
            assertDataFrameEqual(outcome, expected)
        else:
            with pytest.raises(SparkConnectGrpcException):
                users_dataframe.sort('user_id').tail(3)

    @pytest.mark.skip(reason='Not implemented by Spark Connect')
    def test_foreach(self, users_dataframe):
        def noop(_):
            pass

        with utilizes_valid_plans(users_dataframe):
            users_dataframe.limit(3).foreach(noop)

    @pytest.mark.skip(reason='Spark Connect throws an exception on empty tables')
    def test_isempty(self, users_dataframe, spark_session, caplog):
        with utilizes_valid_plans(users_dataframe, caplog):
            outcome = users_dataframe.limit(3)
            assert not outcome.isEmpty()

        with utilizes_valid_plans(users_dataframe, caplog):
            outcome = users_dataframe.limit(0)
            assert outcome.isEmpty()

        with utilizes_valid_plans(users_dataframe, caplog):
            empty = spark_session.createDataFrame([], users_dataframe.schema)
            assert empty.isEmpty()

    def test_select_expr(self, users_dataframe, source, caplog):
        expected = [
            Row(user_id='849118289', b=True),
        ]

        if source == 'spark':
            with utilizes_valid_plans(users_dataframe, caplog):
                outcome = users_dataframe.selectExpr(
                    'substr(user_id, 5, 9)', 'not paid_for_service').limit(1).collect()

            assertDataFrameEqual(outcome, expected)
        else:
            with pytest.raises(SparkConnectGrpcException):
                users_dataframe.selectExpr(
                    'substr(user_id, 5, 9)', 'not paid_for_service').limit(1).collect()

    def test_data_source_schema(self, spark_session):
        location_customer = str(find_tpch() / 'customer')
        schema = spark_session.read.parquet(location_customer).schema
        assert len(schema) == 8

    def test_data_source_filter(self, spark_session):
        location_customer = str(find_tpch() / 'customer')
        customer_dataframe = spark_session.read.parquet(location_customer)

        with utilizes_valid_plans(spark_session):
            outcome = customer_dataframe.filter(col('c_mktsegment') == 'FURNITURE').collect()

        assert len(outcome) == 29968

    def test_table(self, spark_session_with_tpch_dataset):
        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            outcome = spark_session_with_tpch_dataset.table('customer').collect()

        assert len(outcome) == 149999

    def test_table_schema(self, spark_session_with_tpch_dataset):
        schema = spark_session_with_tpch_dataset.table('customer').schema
        assert len(schema) == 8

    def test_table_filter(self, spark_session_with_tpch_dataset):
        customer_dataframe = spark_session_with_tpch_dataset.table('customer')

        with utilizes_valid_plans(spark_session_with_tpch_dataset):
            outcome = customer_dataframe.filter(col('c_mktsegment') == 'FURNITURE').collect()

        assert len(outcome) == 29968

    def test_create_or_replace_temp_view(self, spark_session):
        location_customer = str(find_tpch() / 'customer')
        df_customer = spark_session.read.parquet(location_customer)
        df_customer.createOrReplaceTempView("mytempview")

        with utilizes_valid_plans(spark_session):
            outcome = spark_session.table('mytempview').collect()

        assert len(outcome) == 149999

    def test_create_or_replace_multiple_temp_views(self, spark_session):
        location_customer = str(find_tpch() / 'customer')
        df_customer = spark_session.read.parquet(location_customer)
        df_customer.createOrReplaceTempView("mytempview1")
        df_customer.createOrReplaceTempView("mytempview2")

        with utilizes_valid_plans(spark_session):
            outcome1 = spark_session.table('mytempview1').collect()
            outcome2 = spark_session.table('mytempview2').collect()

        assert len(outcome1) == len(outcome2) == 149999


class TestDataFrameAPIFunctions:
    """Tests functions of the dataframe side of SparkConnect."""

    def test_lit(self, users_dataframe):
        expected = [
            Row(a=42),
            Row(a=42),
            Row(a=42),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(lit(42)).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_broadcast(self, users_dataframe):
        expected = [
            Row(user_id='user849118289', name='Brooke Jones', paid_for_service=False,
                revised_user_id='user849118289', short_name='Brooke'),
        ]

        with utilizes_valid_plans(users_dataframe):
            revised_users_df = users_dataframe.withColumns(
                {'revised_user_id': col('user_id'),
                 'short_name': substring(col('name'), 1, 6),
                 }).drop('user_id', 'name', 'paid_for_service')
            outcome = users_dataframe.join(
                broadcast(revised_users_df),
                revised_users_df.revised_user_id == users_dataframe.user_id).limit(1).collect()

        assertDataFrameEqual(outcome, expected)

    def test_coalesce(self, spark_session):
        expected = [
            Row(a='42.0'),
            Row(a='foo'),
            Row(a=None),
        ]

        string_array = pa.array(['foo', None, None], type=pa.string())
        float_array = pa.array([float('NaN'), 42.0, None], type=pa.float64())
        table = pa.Table.from_arrays([string_array, float_array], names=['s', 'f'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(coalesce('s', 'f')).collect()

        assertDataFrameEqual(outcome, expected)

    def test_isnull(self, spark_session):
        expected = [
            Row(a=False, b=False),
            Row(a=True, b=False),
            Row(a=True, b=True),
        ]

        string_array = pa.array(['foo', None, None], type=pa.string())
        float_array = pa.array([float('NaN'), 42.0, None], type=pa.float64())
        table = pa.Table.from_arrays([string_array, float_array], names=['s', 'f'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(isnull('s'), isnull('f')).collect()

        assertDataFrameEqual(outcome, expected)

    def test_isnan(self, spark_session):
        expected = [
            Row(f=42.0, a=False),
            Row(f=None, a=None),
            Row(f=float('NaN'), a=True),
        ]

        float_array = pa.array([float('NaN'), 42.0, None], type=pa.float64())
        table = pa.Table.from_arrays([float_array], names=['f'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(col('f'), isnan('f')).collect()

        assertDataFrameEqual(outcome, expected)

    def test_nanvl(self, spark_session):
        expected = [
            Row(a=42.0),
            Row(a=9999.0),
            Row(a=None),
        ]

        float_array = pa.array([float('NaN'), 42.0, None], type=pa.float64())
        table = pa.Table.from_arrays([float_array], names=['f'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(nanvl('f', lit(float(9999)))).collect()

        assertDataFrameEqual(outcome, expected)

    def test_expr(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=12),
            Row(name='Collin Frank', a=12),
            Row(name='Joshua Brown', a=12),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select('name', expr('length(name)')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_least(self, spark_session):
        expected = [
            Row(a=-12.0),
            Row(a=42.0),
            Row(a=63.0),
            Row(a=None),
        ]

        float1_array = pa.array([63, 42.0, None, 12], type=pa.float64())
        float2_array = pa.array([float('NaN'), 42.0, None, -12], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array], names=['f1', 'f2'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(least('f1', 'f2')).collect()

        assertDataFrameEqual(outcome, expected)

    def test_greatest(self, spark_session):
        expected = [
            Row(a=12.0),
            Row(a=42.0),
            Row(a=None),
            Row(a=float('NaN')),
        ]

        float1_array = pa.array([63, 42.0, None, 12], type=pa.float64())
        float2_array = pa.array([float('NaN'), 42.0, None, -12], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array], names=['f1', 'f2'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(greatest('f1', 'f2')).collect()

        assertDataFrameEqual(outcome, expected)

    def test_named_struct(self, spark_session):
        expected = [
            Row(named_struct=Row(a='bar', b=2)),
            Row(named_struct=Row(a='foo', b=1)),
        ]

        int_array = pa.array([1, 2], type=pa.int32())
        string_array = pa.array(['foo', 'bar'], type=pa.string())
        table = pa.Table.from_arrays([int_array, string_array], names=['i', 's'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select(named_struct(lit('a'), col('s'), lit('b'), col('i'))).collect()

        assertDataFrameEqual(outcome, expected)

    def test_concat(self, users_dataframe):
        expected = [
            Row(a='user669344115Joshua Browntrue'),
            Row(a='user849118289Brooke Jonesfalse'),
            Row(a='user954079192Collin Frankfalse'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                concat('user_id', 'name', 'paid_for_service')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_equal_null(self, spark_session):
        expected = [
            Row(f1=12.0, f2=-12.0, a=False),
            Row(f1=42.0, f2=42.0, a=True),
            Row(f1=63.0, f2=float('NaN'), a=False),
            Row(f1=None, f2=None, a=True),
        ]

        float1_array = pa.array([63, 42.0, None, 12], type=pa.float64())
        float2_array = pa.array([float('NaN'), 42.0, None, -12], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array], names=['f1', 'f2'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select('f1', 'f2', equal_null('f1', 'f2')).collect()
            assertDataFrameEqual(outcome, expected)

    def test_ifnull(self, spark_session):
        expected = [
            Row(f1=12.0, f2=-12.0, a=12.0),
            Row(f1=None, f2=42.0, a=42.0),
            Row(f1=float('NaN'), f2=63.0, a=float('NaN')),
            Row(f1=None, f2=None, a=None),
        ]

        float1_array = pa.array([float('NaN'), None, None, 12], type=pa.float64())
        float2_array = pa.array([63.0, 42.0, None, -12], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array], names=['f1', 'f2'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select('f1', 'f2', ifnull('f1', 'f2')).collect()
            assertDataFrameEqual(outcome, expected)

    def test_isnotnull(self, spark_session):
        expected = [
            Row(f1=12.0, a=True),
            Row(f1=None, a=False),
            Row(f1=float('NaN'), a=True),
        ]

        float_array = pa.array([float('NaN'), None, 12], type=pa.float64())
        table = pa.Table.from_arrays([float_array], names=['f'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select('f', isnotnull('f')).collect()
            assertDataFrameEqual(outcome, expected)

    def test_nullif(self, spark_session):
        expected = [
            Row(f1=12.0, f2=-12.0, a=12.0),
            Row(f1=12.0, f2=12.0, a=None),
            Row(f1=None, f2=42.0, a=None),
            Row(f1=42.0, f2=42.0, a=None),
            Row(f1=float('NaN'), f2=63.0, a=float('NaN')),
            Row(f1=None, f2=None, a=None),
        ]

        float1_array = pa.array([float('NaN'), 42.0, None, None, 12, 12], type=pa.float64())
        float2_array = pa.array([63.0, 42.0, 42.0, None, -12, 12], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array], names=['f1', 'f2'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select('f1', 'f2', nullif('f1', 'f2')).collect()
            assertDataFrameEqual(outcome, expected)

    def test_nvl(self, spark_session):
        expected = [
            Row(f1=float('NaN'), f2=63.0, a=float('NaN')),
            Row(f1=None, f2=None, a=None),
            Row(f1=None, f2=42.0, a=42.0),
            Row(f1=12.0, f2=-12.0, a=12.0),
            Row(f1=12.0, f2=12.0, a=12.0),
        ]

        float1_array = pa.array([float('NaN'), None, None, 12, 12], type=pa.float64())
        float2_array = pa.array([63.0, 42.0, None, -12, 12], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array], names=['f1', 'f2'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select('f1', 'f2', nvl('f1', 'f2')).collect()
            assertDataFrameEqual(outcome, expected)

    def test_nvl2(self, spark_session):
        expected = [
            Row(f1=float('NaN'), f2=63.0, f3=1.0, a=63.0),
            Row(f1=None, f2=42.0, f3=2.0, a=2.0),
            Row(f1=None, f2=None, f3=3.0, a=3.0),
            Row(f1=12.0, f2=-12.0, f3=4.0, a=-12.0),
        ]

        float1_array = pa.array([float('NaN'), None, None, 12], type=pa.float64())
        float2_array = pa.array([63.0, 42.0, None, -12], type=pa.float64())
        float3_array = pa.array([1, 2, 3, 4], type=pa.float64())
        table = pa.Table.from_arrays([float1_array, float2_array, float3_array],
                                     names=['f1', 'f2', 'f3'])

        df = create_parquet_table(spark_session, 'mytesttable', table)

        with utilizes_valid_plans(df):
            outcome = df.select('f1', 'f2', 'f3', nvl2('f1', 'f2', 'f3')).collect()
            assertDataFrameEqual(outcome, expected)

    def test_bit_length(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=96),
            Row(name='Collin Frank', a=96),
            Row(name='Joshua Brown', a=96),
            Row(name='Mrs. Sheila Jones', a=136),
            Row(name='Rebecca Valentine', a=136),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select('name', bit_length('name')).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_btrim(self, users_dataframe):
        expected = [
            Row(name='rooke Jone'),
            Row(name='Collin Frank'),
            Row(name='Joshua Brown'),
            Row(name='Mrs. Sheila Jone'),
            Row(name='Rebecca Valentine'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(btrim('name', lit('Bs'))).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_character_length(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=12),
            Row(name='Collin Frank', a=12),
            Row(name='Joshua Brown', a=12),
            Row(name='Mrs. Sheila Jones', a=17),
            Row(name='Rebecca Valentine', a=17),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select('name', character_length('name')).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_char_length(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=12),
            Row(name='Collin Frank', a=12),
            Row(name='Joshua Brown', a=12),
            Row(name='Mrs. Sheila Jones', a=17),
            Row(name='Rebecca Valentine', a=17),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select('name', char_length('name')).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_concat_ws(self, users_dataframe):
        expected = [
            Row(a='user669344115|Joshua Brown|true'),
            Row(a='user849118289|Brooke Jones|false'),
            Row(a='user954079192|Collin Frank|false'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                concat_ws('|', 'user_id', 'name', 'paid_for_service')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_contains(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=True),
            Row(name='Collin Frank', a=False),
            Row(name='Joshua Brown', a=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', contains('name', lit('rook'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_endswith(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=False),
            Row(name='Collin Frank', a=True),
            Row(name='Joshua Brown', a=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', endswith('name', lit('Frank'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_instr(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=2),
            Row(name='Collin Frank', a=0),
            Row(name='Joshua Brown', a=0),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', instr('name', 'rook')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_lcase(self, users_dataframe):
        expected = [
            Row(name='brooke jones'),
            Row(name='collin frank'),
            Row(name='joshua brown'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(lcase('name')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_length(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=12),
            Row(name='Collin Frank', a=12),
            Row(name='Joshua Brown', a=12),
            Row(name='Mrs. Sheila Jones', a=17),
            Row(name='Rebecca Valentine', a=17),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select('name', length('name')).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_lower(self, users_dataframe):
        expected = [
            Row(name='brooke jones'),
            Row(name='collin frank'),
            Row(name='joshua brown'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(lower('name')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_left(self, users_dataframe):
        expected = [
            Row(name='Bro'),
            Row(name='Col'),
            Row(name='Jos'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                left('name', lit(3))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_locate(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=9),
            Row(name='Collin Frank', a=0),
            Row(name='Joshua Brown', a=10),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', locate('o', 'name', 5)).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_lpad(self, users_dataframe):
        expected = [
            Row(a='---Brooke Jones'),
            Row(a='---Collin Frank'),
            Row(a='---Joshua Brown'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                lpad('name', 15, '-')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_ltrim(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones'),
            Row(name='Collin Frank'),
            Row(name='Joshua Brown'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                ltrim(lpad('name', 15, ' '))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_octet_length(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=12),
            Row(name='Collin Frank', a=12),
            Row(name='Joshua Brown', a=12),
            Row(name='Mrs. Sheila Jones', a=17),
            Row(name='Rebecca Valentine', a=17),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select('name', octet_length('name')).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_position(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=9),
            Row(name='Collin Frank', a=0),
            Row(name='Joshua Brown', a=10),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', position(lit('o'), 'name', lit(5))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_rlike(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=True),
            Row(name='Collin Frank', a=False),
            Row(name='Joshua Brown', a=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', rlike('name', lit('ro*k'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_regexp(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=True),
            Row(name='Collin Frank', a=False),
            Row(name='Joshua Brown', a=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', regexp('name', lit('ro*k'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_regexp_like(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=True),
            Row(name='Collin Frank', a=False),
            Row(name='Joshua Brown', a=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', regexp_like('name', lit('ro*k'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_replace(self, users_dataframe):
        expected = [
            Row(a='Braake Janes'),
            Row(a='Callin Frank'),
            Row(a='Jashua Brawn'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                replace('name', lit('o'), lit('a'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_right(self, users_dataframe):
        expected = [
            Row(name='nes'),
            Row(name='ank'),
            Row(name='own'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                right('name', lit(3))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_ucase(self, users_dataframe):
        expected = [
            Row(name='BROOKE JONES'),
            Row(name='COLLIN FRANK'),
            Row(name='JOSHUA BROWN'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                ucase('name')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_rpad(self, users_dataframe):
        expected = [
            Row(a='Brooke Jones---'),
            Row(a='Collin Frank---'),
            Row(a='Joshua Brown---'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                rpad('name', 15, '-')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_repeat(self, users_dataframe):
        expected = [
            Row(a='Brooke JonesBrooke Jones'),
            Row(a='Collin FrankCollin Frank'),
            Row(a='Joshua BrownJoshua Brown'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                repeat('name', 2)).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_rtrim(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones'),
            Row(name='Collin Frank'),
            Row(name='Joshua Brown'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                rtrim(rpad('name', 15, ' '))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_startswith(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones', a=True),
            Row(name='Collin Frank', a=False),
            Row(name='Joshua Brown', a=False),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                'name', startswith('name', lit('Bro'))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_substr(self, users_dataframe):
        expected = [
            Row(a='oo'),
            Row(a='ll'),
            Row(a='sh'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                substr('name', lit(3), lit(2))).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_substring(self, users_dataframe):
        expected = [
            Row(a='oo'),
            Row(a='ll'),
            Row(a='sh'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                substring('name', 3, 2)).limit(3).collect()

        assertDataFrameEqual(outcome, expected)

    def test_trim(self, users_dataframe):
        expected = [
            Row(name='Brooke Jones'),
            Row(name='Collin Frank'),
            Row(name='Joshua Brown'),
            Row(name='Mrs. Sheila Jones'),
            Row(name='Rebecca Valentine'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                trim(lpad(rpad('name', 18, ' '), 36, ' '))).limit(5).collect()

        assertDataFrameEqual(outcome, expected)

    def test_upper(self, users_dataframe):
        expected = [
            Row(name='BROOKE JONES'),
            Row(name='COLLIN FRANK'),
            Row(name='JOSHUA BROWN'),
        ]

        with utilizes_valid_plans(users_dataframe):
            outcome = users_dataframe.select(
                upper('name')).limit(3).collect()

        assertDataFrameEqual(outcome, expected)
