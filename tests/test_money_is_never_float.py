"""Guards workspace rule 4: money is integer rupiah, in every language.

The rule is easy to break by accident and very hard to spot in review once a
schema grows, so it is enforced here rather than by discipline. Any field whose
name ends in `_idr` must not be a float, in Pydantic models or in ORM columns.
"""

import importlib
import inspect
import pkgutil
from types import ModuleType

from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, Float, Integer, Numeric
from sqlalchemy.orm import DeclarativeBase

import app

MONEY_SUFFIX = "_idr"
FLOAT_ANNOTATIONS = (float, float | None)


def float_money_fields(model: type[BaseModel]) -> list[str]:
    return [
        name
        for name, field in model.model_fields.items()
        if name.endswith(MONEY_SUFFIX) and field.annotation in FLOAT_ANNOTATIONS
    ]


def float_money_columns(model: type[DeclarativeBase]) -> list[str]:
    # SQLAlchemy models Float as a subclass of Numeric, so testing for Float alone
    # is both necessary and sufficient: DECIMAL columns are Numeric but not Float,
    # and AGENTS.md allows Decimal.
    return [
        column.name
        for column in model.__table__.columns
        if column.name.endswith(MONEY_SUFFIX) and isinstance(column.type, Float)
    ]


def _all_app_modules() -> list[ModuleType]:
    modules = [app]
    for info in pkgutil.walk_packages(app.__path__, prefix=f"{app.__name__}."):
        modules.append(importlib.import_module(info.name))
    return modules


def _classes_of(base: type) -> list[type]:
    found: dict[str, type] = {}
    for module in _all_app_modules():
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, base) and obj is not base:
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(found.values())


def test_detector_catches_a_float_pydantic_field() -> None:
    class Offender(BaseModel):
        selling_price_idr: float
        quantity: float

    assert float_money_fields(Offender) == ["selling_price_idr"]


def test_detector_accepts_integer_rupiah() -> None:
    class Clean(BaseModel):
        selling_price_idr: int
        hpp_idr: int | None = None

    assert float_money_fields(Clean) == []


def test_detector_catches_a_float_orm_column() -> None:
    class OffenderBase(DeclarativeBase):
        pass

    class OffenderTable(OffenderBase):
        __tablename__ = "offender"
        id = Column(Integer, primary_key=True)
        gross_total_idr = Column(Float)
        weight_grams = Column(Float)

    assert float_money_columns(OffenderTable) == ["gross_total_idr"]


def test_detector_accepts_integer_and_decimal_money_columns() -> None:
    class CleanBase(DeclarativeBase):
        pass

    class CleanTable(CleanBase):
        __tablename__ = "clean"
        id = Column(Integer, primary_key=True)
        gross_total_idr = Column(BigInteger)
        hpp_idr = Column(Numeric(18, 0))

    assert float_money_columns(CleanTable) == []


def test_pydantic_money_fields_are_not_float() -> None:
    offenders = [
        f"{model.__module__}.{model.__qualname__}.{name}"
        for model in _classes_of(BaseModel)
        for name in float_money_fields(model)
    ]

    assert not offenders, f"money fields must be int rupiah, found float in: {offenders}"


def test_orm_money_columns_are_not_float() -> None:
    offenders = [
        f"{model.__tablename__}.{name}"  # type: ignore[attr-defined]
        for model in _classes_of(DeclarativeBase)
        if hasattr(model, "__table__")
        for name in float_money_columns(model)
    ]

    assert not offenders, f"money columns must be integer rupiah, found float in: {offenders}"
