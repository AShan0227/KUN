"""Reports generators tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from kun.datamodel.notification import Notification
from kun.engineering.reports import (
    IdleBatchReport,
    IdleBatchReportGenerator,
    IdleBatchStepSummary,
    MonthlyReportGenerator,
    ReportSnapshot,
    WeeklyReportGenerator,
    idle_batch_report_from_steps,
    next_monthly_report_at,
    next_weekly_report_at,
)


class FakeReportDataSource:
    def __init__(self) -> None:
        self.weekly = ReportSnapshot(
            tenant_id="t-1",
            user_id="u-1",
            task_count=12,
            successful_task_count=10,
            consumption_usd=3.4567899,
            saved_usd=9.1,
            learned_items=["写代码前先跑基线测试"],
            system_improvements=["路由规则降本 12%"],
        )
        self.monthly = ReportSnapshot(
            tenant_id="t-1",
            user_id="u-1",
            task_count=40,
            successful_task_count=35,
            consumption_usd=20.0,
            saved_usd=50.0,
            learned_items=["高风险任务先评估"],
            system_improvements=["缓存命中率提升"],
        )
        self.idle = idle_batch_report_from_steps(
            batch_id="batch-1",
            tenant_id="t-1",
            started_at=datetime(2026, 4, 26, 1, tzinfo=UTC),
            finished_at=datetime(2026, 4, 26, 2, 25, tzinfo=UTC),
            steps=[
                IdleBatchStepSummary(step_id="task_replay", status="ok"),
                IdleBatchStepSummary(step_id="knowledge_conflict", status="failed"),
            ],
        )

    async def weekly_snapshot(
        self,
        user_id: str,
        week_start: datetime,
        week_end: datetime,
    ) -> ReportSnapshot:
        assert user_id == "u-1"
        assert week_end - week_start == timedelta(days=7)
        return self.weekly

    async def monthly_snapshot(self, user_id: str, month: date) -> ReportSnapshot:
        assert user_id == "u-1"
        assert month == date(2026, 4, 1)
        return self.monthly

    async def previous_month_consumption(self, user_id: str, month: date) -> float:
        assert user_id == "u-1"
        assert month == date(2026, 4, 1)
        return 18.0

    async def idle_batch_snapshot(self, batch_id: str) -> IdleBatchReport:
        assert batch_id == "batch-1"
        return self.idle


@pytest.mark.asyncio
async def test_weekly_report_generator_summarizes_snapshot() -> None:
    report = await WeeklyReportGenerator(data_source=FakeReportDataSource()).generate(
        "u-1",
        datetime(2026, 4, 20, 9, tzinfo=UTC),
    )

    assert report.tenant_id == "t-1"
    assert report.task_count == 12
    assert report.successful_task_count == 10
    assert report.consumption_usd == 3.45679
    assert report.learned_items == ["写代码前先跑基线测试"]


@pytest.mark.asyncio
async def test_monthly_report_generator_computes_delta() -> None:
    report = await MonthlyReportGenerator(data_source=FakeReportDataSource()).generate(
        "u-1",
        date(2026, 4, 1),
    )

    assert report.task_count == 40
    assert report.month_over_month_delta_usd == 2.0
    assert report.system_improvements == ["缓存命中率提升"]


@pytest.mark.asyncio
async def test_idle_batch_report_generator_returns_snapshot() -> None:
    report = await IdleBatchReportGenerator(data_source=FakeReportDataSource()).generate("batch-1")

    assert report.duration_sec == 5100
    assert report.total_steps == 2
    assert report.failed_steps == 1


@pytest.mark.asyncio
async def test_weekly_report_pushes_notification() -> None:
    pushed: list[Notification] = []

    async def fake_push(notification: Notification) -> None:
        pushed.append(notification)

    notification = await WeeklyReportGenerator(
        data_source=FakeReportDataSource(),
        notification_push=fake_push,
    ).generate_and_push("u-1", datetime(2026, 4, 20, 9, tzinfo=UTC))

    assert notification.kind == "weekly_digest"
    assert notification.tenant_id == "t-1"
    assert pushed == [notification]


@pytest.mark.asyncio
async def test_monthly_report_pushes_notification() -> None:
    pushed: list[Notification] = []

    async def fake_push(notification: Notification) -> None:
        pushed.append(notification)

    notification = await MonthlyReportGenerator(
        data_source=FakeReportDataSource(),
        notification_push=fake_push,
    ).generate_and_push("u-1", date(2026, 4, 1))

    assert notification.kind == "monthly_report"
    assert "环比 $2.00" in notification.body
    assert pushed == [notification]


@pytest.mark.asyncio
async def test_idle_batch_report_pushes_notification() -> None:
    pushed: list[Notification] = []

    async def fake_push(notification: Notification) -> None:
        pushed.append(notification)

    notification = await IdleBatchReportGenerator(
        data_source=FakeReportDataSource(),
        notification_push=fake_push,
    ).generate_and_push("batch-1")

    assert notification.kind == "idle_batch_report"
    assert notification.payload["failed_steps"] == 1
    assert pushed == [notification]


def test_idle_batch_report_from_steps_counts_failures() -> None:
    report = idle_batch_report_from_steps(
        batch_id="b",
        tenant_id="t",
        started_at=datetime(2026, 4, 26, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 26, 0, 1, tzinfo=UTC),
        steps=[
            IdleBatchStepSummary(step_id="a", status="ok"),
            IdleBatchStepSummary(step_id="b", status="failed"),
        ],
    )

    assert report.duration_sec == 60
    assert report.total_steps == 2
    assert report.failed_steps == 1


def test_next_weekly_report_is_monday_9am() -> None:
    scheduled = next_weekly_report_at(datetime(2026, 4, 26, 12, tzinfo=UTC))

    assert scheduled == datetime(2026, 4, 27, 9, tzinfo=UTC)


def test_next_monthly_report_is_first_day_9am() -> None:
    scheduled = next_monthly_report_at(datetime(2026, 4, 26, 12, tzinfo=UTC))

    assert scheduled == datetime(2026, 5, 1, 9, tzinfo=UTC)
