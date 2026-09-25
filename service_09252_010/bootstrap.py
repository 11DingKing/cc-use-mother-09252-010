"""组装根：选择端口、装载快照、初始化服务与种子数据。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .application.access import AccessControl
from .application.calc import CalculationService
from .application.catalog import CatalogService
from .application.export import ExportService
from .application.importer import ImportService
from .application.review import ReviewService
from .domain import models as m
from .infrastructure.adapters import (JsonSnapshotStore, SequenceIdGenerator,
                                      SystemClock)
from .persistence.uow import Database, UnitOfWork


@dataclass
class Container:
    db: Database
    store: Optional[object]
    clock: object
    idgen: object
    access: AccessControl
    catalog: CatalogService
    importer: ImportService
    calc: CalculationService
    review: ReviewService
    export: ExportService

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.db, self.store)


def build_container(snapshot_path: Optional[str] = None,
                    clock=None, idgen=None, seed: bool = True) -> Container:
    clock = clock or SystemClock()
    idgen = idgen or SequenceIdGenerator(clock)
    store = JsonSnapshotStore(snapshot_path) if snapshot_path else None

    db = Database()
    db.meta["clock"] = clock
    db.meta["idgen"] = idgen
    if store is not None:
        snap = store.load()
        if snap is not None:
            db = Database.from_snapshot(snap)
            db.meta["clock"] = clock
            db.meta["idgen"] = idgen

    catalog = CatalogService()
    importer = ImportService()
    calc = CalculationService(store=store)
    review = ReviewService()
    exporter = ExportService()
    access = AccessControl(db)
    container = Container(db=db, store=store, clock=clock, idgen=idgen,
                          access=access, catalog=catalog, importer=importer,
                          calc=calc, review=review, export=exporter)
    if seed and not db.orgs:
        _seed(container)
    return container


def _seed(c: Container) -> None:
    """内置演示主体与授权；真实部署可通过环境变量替换快照路径。

    授权粒度：scope × 项目。例如 A 国机构仅能向其项目导入数据，
    会签机构仅有 signoff 粒度，复核机构仅有 review 粒度。
    """
    with c.uow() as db:
        db.orgs["org-a"] = m.Org("org-a", "A国职教合作局", "CN")
        db.orgs["org-b"] = m.Org("org-b", "B国技能署", "DE")
        db.orgs["org-c"] = m.Org("org-c", "C国教育部", "KE")
        db.orgs["org-audit"] = m.Org("org-audit", "独立复核中心", "INT")

        db.projects["prj-demo"] = m.Project(
            "prj-demo", "中德职业教育合作示范项目", "org-a",
            ["CN", "DE"], c.clock.now())

        # 管理员：全粒度
        db.principals["u-admin"] = m.Principal(
            "u-admin", "管理员", "org-a",
            grants=[(m.SCOPE_ADMIN, None)], tokens=["token-admin"])
        # A 国机构：仅对 prj-demo 有导入与读
        db.principals["u-a"] = m.Principal(
            "u-a", "A国经办人", "org-a",
            grants=[(m.SCOPE_IMPORT, "prj-demo"), (m.SCOPE_READ, "prj-demo"),
                    (m.SCOPE_SIGNOFF, None)],
            tokens=["token-a"])
        # B 国机构：仅对 prj-demo 导入/读/会签
        db.principals["u-b"] = m.Principal(
            "u-b", "B国经办人", "org-b",
            grants=[(m.SCOPE_IMPORT, "prj-demo"), (m.SCOPE_READ, "prj-demo"),
                    (m.SCOPE_SIGNOFF, None)],
            tokens=["token-b"])
        # 复核中心：仅复核粒度
        db.principals["u-audit"] = m.Principal(
            "u-audit", "复核员", "org-audit",
            grants=[(m.SCOPE_REVIEW, "prj-demo"), (m.SCOPE_READ, "prj-demo")],
            tokens=["token-audit"])
        # 计算主体：仅计算粒度
        db.principals["u-calc"] = m.Principal(
            "u-calc", "计算调度", "org-audit",
            grants=[(m.SCOPE_CALC, "prj-demo"), (m.SCOPE_READ, "prj-demo")],
            tokens=["token-calc"])


def default_snapshot_path() -> str:
    return os.environ.get("EFFECT_SNAPSHOT_PATH", "./data/snapshot.json")
