"""三层透明化报告生成器 (BATCH4 C5 / T19+T20)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.datamodel.notification import Notification
from kun.engineering.notifications import push as default_push


class ReportSnapshot(BaseModel):
    """周/月报告通用汇总输入."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    task_count: int = 0
    successful_task_count: int = 0
    consumption_usd: float = 0.0
    saved_usd: float = 0.0
    learned_items: list[str] = Field(default_factory=list)
    system_improvements: list[str] = Field(default_factory=list)


class WeeklyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    week_start: datetime
    week_end: datetime
    task_count: int
    successful_task_count: int
    consumption_usd: float
    saved_usd: float
    learned_items: list[str]
    system_improvements: list[str]


class MonthlyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    month: date
    task_count: int
    successful_task_count: int
    consumption_usd: float
    saved_usd: float
    month_over_month_delta_usd: float = 0.0
    learned_items: list[str]
    system_improvements: list[str]


class IdleBatchStepSummary(BaseModel):
    step_id: str
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)


class IdleBatchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    tenant_id: str
    started_at: datetime
    finished_at: datetime
    duration_sec: float
    steps: list[IdleBatchStepSummary]
    total_steps: int
    failed_steps: int


class ReportDataSource(Protocol):
    async def weekly_snapshot(
        self,
        user_id: str,
        week_start: datetime,
        week_end: datetime,
    ) -> ReportSnapshot: ...

    async def monthly_snapshot(self, user_id: str, month: date) -> ReportSnapshot: ...

    async def previous_month_consumption(self, user_id: str, month: date) -> float: ...

    async def idle_batch_snapshot(self, batch_id: str) -> IdleBatchReport: ...


NotificationPusher = Callable[[Notification], Awaitable[None]]


class EmptyReportDataSource:
    """默认空数据源. 生产接真实 DB 前不会编造数据."""

    async def weekly_snapshot(
        self,
        user_id: str,
        week_start: datetime,
        week_end: datetime,
    ) -> ReportSnapshot:
        return ReportSnapshot(tenant_id=user_id, user_id=user_id)

    async def monthly_snapshot(self, user_id: str, month: date) -> ReportSnapshot:
        return ReportSnapshot(tenant_id=user_id, user_id=user_id)

    async def previous_month_consumption(self, user_id: str, month: date) -> float:
        return 0.0

    async def idle_batch_snapshot(self, batch_id: str) -> IdleBatchReport:
        now = datetime.now(UTC)
        return IdleBatchReport(
            batch_id=batch_id,
            tenant_id="unknown",
            started_at=now,
            finished_at=now,
            duration_sec=0.0,
            steps=[],
            total_steps=0,
            failed_steps=0,
        )


class WeeklyReportGenerator:
    def __init__(
        self,
        *,
        data_source: ReportDataSource | None = None,
        notification_push: NotificationPusher = default_push,
    ) -> None:
        self._data_source = data_source or EmptyReportDataSource()
        self._push = notification_push

    async def generate(self, user_id: str, week_start: datetime) -> WeeklyReport:
        week_start = _ensure_utc(week_start)
        week_end = week_start + timedelta(days=7)
        snapshot = await self._data_source.weekly_snapshot(user_id, week_start, week_end)
        return WeeklyReport(
            user_id=user_id,
            tenant_id=snapshot.tenant_id,
            week_start=week_start,
            week_end=week_end,
            task_count=snapshot.task_count,
            successful_task_count=snapshot.successful_task_count,
            consumption_usd=_money(snapshot.consumption_usd),
            saved_usd=_money(snapshot.saved_usd),
            learned_items=snapshot.learned_items,
            system_improvements=snapshot.system_improvements,
        )

    async def generate_and_push(self, user_id: str, week_start: datetime) -> Notification:
        report = await self.generate(user_id, week_start)
        notification = _notification_from_report(
            tenant_id=report.tenant_id,
            kind="weekly_digest",
            title="本周 KUN 透明报告",
            body=(
                f"本周完成 {report.task_count} 个任务, "
                f"消费 ${report.consumption_usd:.2f}, 节省 ${report.saved_usd:.2f}。"
            ),
            payload=report.model_dump(mode="json"),
        )
        await self._push(notification)
        return notification


class MonthlyReportGenerator:
    def __init__(
        self,
        *,
        data_source: ReportDataSource | None = None,
        notification_push: NotificationPusher = default_push,
    ) -> None:
        self._data_source = data_source or EmptyReportDataSource()
        self._push = notification_push

    async def generate(self, user_id: str, month: date) -> MonthlyReport:
        snapshot = await self._data_source.monthly_snapshot(user_id, month)
        previous = await self._data_source.previous_month_consumption(user_id, month)
        delta = snapshot.consumption_usd - previous
        return MonthlyReport(
            user_id=user_id,
            tenant_id=snapshot.tenant_id,
            month=month,
            task_count=snapshot.task_count,
            successful_task_count=snapshot.successful_task_count,
            consumption_usd=_money(snapshot.consumption_usd),
            saved_usd=_money(snapshot.saved_usd),
            month_over_month_delta_usd=_money(delta),
            learned_items=snapshot.learned_items,
            system_improvements=snapshot.system_improvements,
        )

    async def generate_and_push(self, user_id: str, month: date) -> Notification:
        report = await self.generate(user_id, month)
        notification = _notification_from_report(
            tenant_id=report.tenant_id,
            kind="monthly_report",
            title="本月 KUN 透明报告",
            body=(
                f"本月完成 {report.task_count} 个任务, "
                f"消费 ${report.consumption_usd:.2f}, 环比 ${report.month_over_month_delta_usd:.2f}。"
            ),
            payload=report.model_dump(mode="json"),
        )
        await self._push(notification)
        return notification


class IdleBatchReportGenerator:
    def __init__(
        self,
        *,
        data_source: ReportDataSource | None = None,
        notification_push: NotificationPusher = default_push,
    ) -> None:
        self._data_source = data_source or EmptyReportDataSource()
        self._push = notification_push

    async def generate(self, batch_id: str) -> IdleBatchReport:
        return await self._data_source.idle_batch_snapshot(batch_id)

    async def generate_and_push(self, batch_id: str) -> Notification:
        report = await self.generate(batch_id)
        notification = _notification_from_report(
            tenant_id=report.tenant_id,
            kind="idle_batch_report",
            title="夜间作业完成",
            body=(
                f"夜间作业 {batch_id} 跑了 {report.total_steps} 步, "
                f"失败 {report.failed_steps} 步, 用时 {report.duration_sec:.0f} 秒。"
            ),
            payload=report.model_dump(mode="json"),
        )
        await self._push(notification)
        return notification


def next_weekly_report_at(now: datetime, *, hour: int = 9) -> datetime:
    """下一个周一 9 点 (用户时区由调用方先转换)."""

    now = _ensure_utc(now)
    days_until_monday = (7 - now.weekday()) % 7
    candidate = datetime.combine((now + timedelta(days=days_until_monday)).date(), time(hour), UTC)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def next_monthly_report_at(now: datetime, *, hour: int = 9) -> datetime:
    """下个月 1 号 9 点 (用户时区由调用方先转换)."""

    now = _ensure_utc(now)
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    candidate = datetime(year, month, 1, hour, tzinfo=UTC)
    if now.day == 1 and now.hour < hour:
        candidate = datetime(now.year, now.month, 1, hour, tzinfo=UTC)
    return candidate


def idle_batch_report_from_steps(
    *,
    batch_id: str,
    tenant_id: str,
    started_at: datetime,
    finished_at: datetime,
    steps: Sequence[IdleBatchStepSummary],
) -> IdleBatchReport:
    started_at = _ensure_utc(started_at)
    finished_at = _ensure_utc(finished_at)
    step_list = list(steps)
    return IdleBatchReport(
        batch_id=batch_id,
        tenant_id=tenant_id,
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=max((finished_at - started_at).total_seconds(), 0.0),
        steps=step_list,
        total_steps=len(step_list),
        failed_steps=sum(1 for step in step_list if step.status == "failed"),
    )


def _notification_from_report(
    *,
    tenant_id: str,
    kind: str,
    title: str,
    body: str,
    payload: dict[str, Any],
) -> Notification:
    return Notification(
        tenant_id=tenant_id,
        kind=kind,
        severity="info",
        channel="side",
        title=title,
        body=body,
        payload=payload,
        render_hint={"collapsed": True, "pin": False, "muted_ok": True},
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _money(value: float) -> float:
    return round(value, 6)
