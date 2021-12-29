from uuid import uuid4
import pandas as pd
import os

from .utils import (
    assert_datatypes,
    column_filters,
    handle_limit,
    handle_sort,
    hydrate,
    is_datetime,
    parse_datatype,
)

from .models import ModelManager, Model

# TODO Remove this and solve all chained assingments.
pd.options.mode.chained_assignment = None


# TODO: Dynamically compile database at runtime utelizing __slots__ to store tables.
class Database:
    def __init__(self, models: ModelManager = ModelManager(), path: str = ''):
        '''
        Simple in-memory database built with pandas to store data in ram.
        This is a "Pandas Database".
        '''
        self.models = models
        # TODO ensure this is OS compatible.
        self.path = path

        self.load()

    def __setitem__(self, key, value):
        self.__dict__[key] = value

    def __getitem__(self, key):
        return self.__dict__[key]

    def has(self, model_name: str) -> bool:
        if hasattr(self, model_name):
            if isinstance(self[model_name], pd.DataFrame):
                return True

        return False

    def create(self, model_name, **kwargs) -> Model:
        instance = self.models[model_name](**kwargs)
        datatypes = instance._schema.datatypes()
        for key, value in kwargs.items():
            assert_datatypes(self, datatypes[key], value, key)

        df = instance._to_df()

        if self.has(model_name):
            self[model_name] = self[model_name].append(df, ignore_index=True)
        else:
            self[model_name] = df
        return instance

    def query(self, model_name: str, **kwargs) -> pd.DataFrame:
        if self.has(model_name):
            df = self[model_name]
            keys = list(kwargs.keys())
            for key in keys:
                if key not in df.columns:
                    del kwargs[key]

            kwargs, df = handle_sort(kwargs, df)
            kwargs, df = handle_limit(kwargs, df)

            for key, value in kwargs.items():
                if isinstance(value, (list, dict)):
                    raise TypeError('Nested value filtering is not supported.')

                if '__' in key:

                    column, operator = key.split('__', 1)
                    if is_datetime(value):
                        value = pd.to_datetime(value, utc=True)
                        df[column] = pd.to_datetime(df[column])

                    # FK lookup.
                    if foreign_model := self.models[model_name].datatypes()[column] in self.models:
                        fk_df = self.query(foreign_model.__name__, **{operator: value}).pk
                        temp_column_suffix = f'_{uuid4()}'
                        df = pd.merge(left=df, right=fk_df, how='right', left_on=column, right_on='pk',
                                      suffixes=(None, temp_column_suffix))
                        df.drop(f'pk{temp_column_suffix}', inplace=True, axis=1)

                    # Special filters
                    else:
                        if operator == 'includes':
                            #TODO -- complete this
                            ...
                        df = column_filters[operator](df, column, value)

                    if is_datetime(value):
                        df[column] = df[column].dt.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    df = df[df[key] == value]
            return df
        else:
            return pd.DataFrame()

    def get(self, model_name, pk: str):
        df = self.query(model_name, pk=pk)
        if not df.empty:
            return self.models[model_name](**df.iloc[0].to_dict())

        return None

    def update(self, model_name: str, query: pd.DataFrame, **kwargs):
        instance = self.models[model_name](**kwargs)
        datatypes = instance._schema.datatypes()

        for key, value in kwargs.items():
            assert_datatypes(self, datatypes[key], value, key)

        for field, value in kwargs.items():
            self[model_name].loc[query.index, field] = [value]

        return self[model_name].iloc[query.index]

    def drop(self, model_name: str, **kwargs) -> None:
        indexes = self.query(model_name, **kwargs).index
        self[model_name].drop(index=indexes, inplace=True)

    def hydrate(self, model_name: str, **kwargs):
        return hydrate(self, model_name, self.query(model_name, **kwargs))

    def init_table(self, model):
        self[model.__name__] = model()._to_df().iloc[0:0]

    def audit_tables(self):
        for model in self.models:
            if not self.has(model.__name__):
                self.init_table(model)

    def audit_nulls(self):
        for model in self.models:
            instance = model()
            for field, datatype in instance._schema.datatypes().items():
                nulls_index = self[instance._name][field].isnull()
                if nulls_index.sum() and datatype not in self.models:
                    self[instance._name].loc[nulls_index, field] = pd.Series([datatype()] * nulls_index.sum())

    def init_datatypes(self, model):
        instance = model()
        for field, datatype, default_value in instance._schema.items():
            self[instance._name][field].apply(lambda value: parse_datatype(self, datatype, value))

    def audit_datatypes(self):
        for model in self.models:
            self.init_datatypes(model)

    def init_fields(self, model):
        instance = model()
        if self.has(instance._name):
            loaded_fields = set(self[instance._name].columns)
        else:
            loaded_fields = set()
        model_fields = set([field for field in instance._schema.fields()])
        new_fields = model_fields.difference(loaded_fields)
        removed_fields = loaded_fields.difference(model_fields)

        for field in new_fields:
            self[instance._name][field] = None

        for field in removed_fields:
            self[instance._name] = self[instance._name].drop(field, axis=1)

    def audit_fields(self):
        for model in self.models:
            self.init_fields(model)

    def migrate(self):
        self.audit_tables()
        self.audit_fields()
        self.audit_nulls()
        self.audit_datatypes()

    def save(self):
        for model in self.models:
            json_file_path = os.path.join(self.path, f'{model.__name__}.json')
            self[model.__name__].to_json(json_file_path, orient='records', indent=4)

    def load(self):
        for model in self.models:
            json_file_path = os.path.join(self.path, f'{model.__name__}.json')
            if os.path.isfile(json_file_path):
                self[model.__name__] = pd.read_json(json_file_path, orient='records', convert_dates=False)
