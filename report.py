#!/usr/bin/env python3
"""Build per-work and per-contract payment reports from CSV exports."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


ZERO = Decimal("0.00")
VALID_WORK_STATUSES = {"planned", "done", "cancelled"}
VALID_PAYMENT_STATUSES = {"paid", "queued", "refunded"}


class DataError(ValueError):
    """Raised when an input export is inconsistent or malformed."""


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: date | None
    cancelled_at: date | None


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: Decimal
    status: str
    created_at: date
    paid_at: date | None
    refunded_at: date | None


@dataclass(frozen=True)
class WorkResult:
    report_date: date
    contract_id: str
    contractor: str
    work_id: str
    title: str
    status_as_of: str
    price: Decimal
    payable_price: Decimal
    paid: Decimal
    remaining: Decimal


@dataclass(frozen=True)
class ContractResult:
    report_date: date
    contract_id: str
    contractor: str
    contract_amount: Decimal
    payable_works_total: Decimal
    cancelled_works_total: Decimal
    paid: Decimal
    unallocated_paid: Decimal
    remaining: Decimal
    overpaid: Decimal


def parse_date(value: str, field: str, *, optional: bool = False) -> date | None:
    value = value.strip()
    if not value and optional:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataError(f"Некорректная дата в {field}: {value!r}") from exc


def parse_money(value: str, field: str) -> Decimal:
    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise DataError(f"Некорректная сумма в {field}: {value!r}") from exc
    if not amount.is_finite() or amount < ZERO:
        raise DataError(f"Сумма в {field} должна быть конечной и неотрицательной")
    if amount.as_tuple().exponent < -2:
        raise DataError(f"Сумма в {field} содержит больше двух знаков после запятой")
    return amount.quantize(Decimal("0.01"))


def read_rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise DataError(
                    f"В {path} отсутствуют колонки: {', '.join(sorted(missing))}"
                )
            return list(reader)
    except OSError as exc:
        raise DataError(f"Не удалось прочитать {path}: {exc}") from exc


def load_data(data_dir: Path) -> tuple[list[Contract], list[Work], list[Payment]]:
    contract_rows = read_rows(
        data_dir / "contracts.csv", {"contract_id", "contractor", "amount", "signed_at"}
    )
    work_rows = read_rows(
        data_dir / "works.csv",
        {"work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"},
    )
    payment_rows = read_rows(
        data_dir / "payments.csv",
        {
            "payment_id", "contract_id", "work_id", "purpose", "amount", "status",
            "created_at", "paid_at", "refunded_at",
        },
    )

    contracts = [
        Contract(
            row["contract_id"].strip(),
            row["contractor"].strip(),
            parse_money(row["amount"], f"contracts.csv:{index}:amount"),
            required_date(row["signed_at"], f"contracts.csv:{index}:signed_at"),
        )
        for index, row in enumerate(contract_rows, start=2)
    ]
    works = [
        Work(
            row["work_id"].strip(),
            row["contract_id"].strip(),
            row["title"].strip(),
            parse_money(row["price"], f"works.csv:{index}:price"),
            row["status"].strip(),
            parse_date(row["completed_at"], f"works.csv:{index}:completed_at", optional=True),
            parse_date(row["cancelled_at"], f"works.csv:{index}:cancelled_at", optional=True),
        )
        for index, row in enumerate(work_rows, start=2)
    ]
    payments = [
        Payment(
            row["payment_id"].strip(),
            row["contract_id"].strip(),
            row["work_id"].strip() or None,
            row["purpose"].strip(),
            parse_money(row["amount"], f"payments.csv:{index}:amount"),
            row["status"].strip(),
            required_date(row["created_at"], f"payments.csv:{index}:created_at"),
            parse_date(row["paid_at"], f"payments.csv:{index}:paid_at", optional=True),
            parse_date(row["refunded_at"], f"payments.csv:{index}:refunded_at", optional=True),
        )
        for index, row in enumerate(payment_rows, start=2)
    ]
    validate_data(contracts, works, payments)
    return contracts, works, payments


def required_date(value: str, field: str) -> date:
    parsed = parse_date(value, field)
    if parsed is None:  # Defensive: non-optional parse_date never returns None.
        raise DataError(f"Пустая обязательная дата в {field}")
    return parsed


def validate_unique(items: Iterable[str], label: str) -> set[str]:
    seen: set[str] = set()
    for item in items:
        if not item:
            raise DataError(f"Пустой идентификатор: {label}")
        if item in seen:
            raise DataError(f"Дублирующийся {label}: {item}")
        seen.add(item)
    return seen


def validate_data(
    contracts: list[Contract], works: list[Work], payments: list[Payment]
) -> None:
    contract_ids = validate_unique((item.contract_id for item in contracts), "contract_id")
    work_ids = validate_unique((item.work_id for item in works), "work_id")
    validate_unique((item.payment_id for item in payments), "payment_id")
    work_contract = {item.work_id: item.contract_id for item in works}

    for work in works:
        if work.contract_id not in contract_ids:
            raise DataError(f"Работа {work.work_id} ссылается на неизвестный договор")
        if work.status not in VALID_WORK_STATUSES:
            raise DataError(f"Неизвестный статус работы {work.work_id}: {work.status}")
        if work.status == "done" and work.completed_at is None:
            raise DataError(f"У выполненной работы {work.work_id} нет completed_at")
        if work.status == "cancelled" and work.cancelled_at is None:
            raise DataError(f"У отменённой работы {work.work_id} нет cancelled_at")

    for payment in payments:
        if payment.contract_id not in contract_ids:
            raise DataError(f"Платёж {payment.payment_id} ссылается на неизвестный договор")
        if payment.status not in VALID_PAYMENT_STATUSES:
            raise DataError(f"Неизвестный статус платежа {payment.payment_id}: {payment.status}")
        if payment.status in {"paid", "refunded"} and payment.paid_at is None:
            raise DataError(f"У платежа {payment.payment_id} нет paid_at")
        if payment.status == "refunded" and payment.refunded_at is None:
            raise DataError(f"У возвращённого платежа {payment.payment_id} нет refunded_at")
        if payment.work_id:
            if payment.work_id not in work_ids:
                raise DataError(f"Платёж {payment.payment_id} ссылается на неизвестную работу")
            if work_contract[payment.work_id] != payment.contract_id:
                raise DataError(
                    f"Платёж {payment.payment_id} и работа {payment.work_id} относятся к разным договорам"
                )


def work_status_as_of(work: Work, report_date: date) -> str:
    if work.cancelled_at is not None and work.cancelled_at <= report_date:
        return "cancelled"
    if work.completed_at is not None and work.completed_at <= report_date:
        return "done"
    return "planned"


def payment_effect_as_of(payment: Payment, report_date: date) -> Decimal:
    if payment.status == "queued" or payment.paid_at is None or payment.paid_at > report_date:
        return ZERO
    if payment.status == "refunded" and payment.refunded_at is not None:
        if payment.refunded_at <= report_date:
            return ZERO
    return payment.amount


def calculate_report(
    contracts: list[Contract],
    works: list[Work],
    payments: list[Payment],
    report_date: date,
) -> tuple[list[WorkResult], list[ContractResult]]:
    works_by_contract: dict[str, list[Work]] = defaultdict(list)
    payments_by_contract: dict[str, list[Payment]] = defaultdict(list)
    for work in works:
        works_by_contract[work.contract_id].append(work)
    for payment in payments:
        payments_by_contract[payment.contract_id].append(payment)

    work_results: list[WorkResult] = []
    contract_results: list[ContractResult] = []

    for contract in sorted(contracts, key=lambda item: item.contract_id):
        if contract.signed_at > report_date:
            continue

        contract_works = works_by_contract[contract.contract_id]
        statuses = {work.work_id: work_status_as_of(work, report_date) for work in contract_works}
        payable = {
            work.work_id: (ZERO if statuses[work.work_id] == "cancelled" else work.price)
            for work in contract_works
        }
        paid_by_work: dict[str, Decimal] = defaultdict(lambda: ZERO)
        unbound_paid = ZERO
        contract_paid = ZERO

        for payment in payments_by_contract[contract.contract_id]:
            effect = payment_effect_as_of(payment, report_date)
            if effect == ZERO:
                continue
            contract_paid += effect
            if payment.work_id:
                paid_by_work[payment.work_id] += effect
            else:
                unbound_paid += effect

        ordered_works = sorted(
            (work for work in contract_works if statuses[work.work_id] != "cancelled"),
            key=lambda work: (
                0 if statuses[work.work_id] == "done" else 1,
                work.completed_at or date.max,
                work.work_id,
            ),
        )
        for work in ordered_works:
            capacity = max(payable[work.work_id] - paid_by_work[work.work_id], ZERO)
            allocation = min(capacity, unbound_paid)
            paid_by_work[work.work_id] += allocation
            unbound_paid -= allocation
            if unbound_paid == ZERO:
                break

        for work in sorted(contract_works, key=lambda item: item.work_id):
            remaining = max(payable[work.work_id] - paid_by_work[work.work_id], ZERO)
            work_results.append(
                WorkResult(
                    report_date=report_date,
                    contract_id=contract.contract_id,
                    contractor=contract.contractor,
                    work_id=work.work_id,
                    title=work.title,
                    status_as_of=statuses[work.work_id],
                    price=work.price,
                    payable_price=payable[work.work_id],
                    paid=paid_by_work[work.work_id],
                    remaining=remaining,
                )
            )

        payable_total = sum(payable.values(), ZERO)
        cancelled_total = sum(
            (work.price for work in contract_works if statuses[work.work_id] == "cancelled"),
            ZERO,
        )
        contract_results.append(
            ContractResult(
                report_date=report_date,
                contract_id=contract.contract_id,
                contractor=contract.contractor,
                contract_amount=contract.amount,
                payable_works_total=payable_total,
                cancelled_works_total=cancelled_total,
                paid=contract_paid,
                unallocated_paid=unbound_paid,
                remaining=max(payable_total - contract_paid, ZERO),
                overpaid=max(contract_paid - payable_total, ZERO),
            )
        )

    return work_results, contract_results


def money_text(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def write_reports(
    output_dir: Path,
    work_results: list[WorkResult],
    contract_results: list[ContractResult],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    works_path = output_dir / "works_report.csv"
    contracts_path = output_dir / "contracts_report.csv"

    with works_path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(
            [
                "report_date", "contract_id", "contractor", "work_id", "title",
                "status_as_of", "price", "payable_price", "paid", "remaining",
            ]
        )
        for row in work_results:
            writer.writerow(
                [
                    row.report_date.isoformat(), row.contract_id, row.contractor, row.work_id,
                    row.title, row.status_as_of, money_text(row.price),
                    money_text(row.payable_price), money_text(row.paid), money_text(row.remaining),
                ]
            )

    with contracts_path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(
            [
                "report_date", "contract_id", "contractor", "contract_amount",
                "payable_works_total", "cancelled_works_total", "paid",
                "unallocated_paid", "remaining", "overpaid",
            ]
        )
        for row in contract_results:
            writer.writerow(
                [
                    row.report_date.isoformat(), row.contract_id, row.contractor,
                    money_text(row.contract_amount), money_text(row.payable_works_total),
                    money_text(row.cancelled_works_total), money_text(row.paid),
                    money_text(row.unallocated_paid), money_text(row.remaining),
                    money_text(row.overpaid),
                ]
            )

    return works_path, contracts_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Отчёт по оплате работ на выбранную дату")
    parser.add_argument("--date", required=True, dest="report_date", help="Дата отчёта, ГГГГ-ММ-ДД")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Каталог входных CSV")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("report"), help="Каталог для результатов"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report_date = required_date(args.report_date, "--date")
        contracts, works, payments = load_data(args.data_dir)
        work_results, contract_results = calculate_report(
            contracts, works, payments, report_date
        )
        works_path, contracts_path = write_reports(
            args.output_dir, work_results, contract_results
        )
    except DataError as exc:
        print(f"Ошибка данных: {exc}", file=sys.stderr)
        return 2

    print(f"Отчёт по работам: {works_path}")
    print(f"Итоги по договорам: {contracts_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
